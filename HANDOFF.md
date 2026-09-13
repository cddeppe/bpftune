## SESSION 2026-09-13 — mid-flight sampling diagnostic

**Branch:** `diag/metric-terms` | **Fleet:** 0.4.19 on heavy host, 0.4.18 elsewhere | **Last tag:** `0.4-9-custom`

### What's deployed
0.4.19 on the aarch64 heavy-traffic host only. Adds a mid-flight diagnostic:
- `midsamp port=P thr=T segs=N smin=M savg=A srate=R` at 1K/5K/25K/100K/500K/1M segment checkpoints (RTT_CB)
- `closport port=P segs=N` for pairing

### Problem being diagnosed
Metric only updates at socket close. Long-lived sockets (video streams) rarely close, so the tuner learns mostly from control traffic. Need fixed-size sampling instead of close-only.

### Confirmed finding
Queueing delay (savg - smin) grows with socket size — 10-13K µs at 1000 segs vs 19-20K at 5000 segs. RTT term measures "how big was the socket," not algorithm quality. Fixed-size sampling justified.

### Next step
Let a YouTube stream run 10 minutes on the heavy host, then:
    sudo grep -c 'midsamp' /tmp/met.log
    sudo grep 'midsamp' /tmp/met.log | awk '{for(i=1;i<=NF;i++){if($i~/^thr=/){split($i,a,"="); print a[2]}}}' | sort -n | uniq -c
    sudo head -3 /tmp/met.log
- 25K+ checkpoints fire → design fixed-size sampling
- only 1K/5K fire → long sockets missed, investigate that
- LOST EVENTS in header → increase buffer_size_kb

### Known limitation
`ops->local_port` is 443 for all home connections — midsamp/closport can't pair. Next patch: add `ops->remote_port` to both printks.

### Fixes shipped on this branch since 0.4.9
Rate-term inert; small-socket RTT poisoning; rate-term frozen per algorithm; RTT term path-constant; outlier rejection; reference healing; destination keying (2 buckets → 100+ per host).

### Parked
Reference drift churn; multi-netns map dump; link-local filter; branch merge to main + 0.4.20 release.

---

## CURRENT STATE — post-metric-fix session

**Version:** 0.4.14 on `diag/metric-terms` branch. `main` is at 0.4.9 (released as `0.4-9-custom`).
**Fleet:** all four hosts on 0.4.14 with diagnostic captures running. All state files reset on deploy.

### Branches
- `main` — 0.4.9, released, latest tag `0.4-9-custom`
- `diag/metric-terms` — 0.4.10 through 0.4.14, un-merged. Contains all metric fixes since 0.4.9.

### What was fixed this session (three bugs + one redesign)

1. **0.4.10** — diagnostic printk extended. Instrumentation only.
2. **0.4.11** — printk extended further (added `smrtt`/`bmrtt`) and duplicate old-format printk removed.
3. **0.4.12** — segment threshold on metric updates (`METRIC_MIN_SEGS=100`).
   Small sockets (health-check cadence, 16-36 segments) were updating the bucket's
   `min_rtt` reference and pinning it to 144µs, while real traffic was ~30000µs.
   Every real socket then hit the RTT cap and the tuner locked onto whatever
   algorithm got the poisoned bucket first.
4. **0.4.13** — rate term passes the current socket's `rate_delivered` instead of the
   algorithm's all-time-best. Previously the term was frozen per algorithm
   (`m->max_rate_delivered` only changed when the alg set a new personal best).
   Same algorithm on sockets of 30x different size produced identical `rate_term`.
5. **0.4.14** — RTT term changed from min-vs-min to queueing-delay:
   `avg_rtt - bucket_min_rtt`. min-vs-min was path-constant (all 16 algs within
   ±5% of each other). Queueing delay is algorithm-dependent (5-38ms spread observed;
   dctcp/vegas shallow, hybla/scalable deep — matches theory).

### The current metric (0.4.14)

    metric = rtt_term + rate_term         (lower is better; chosen alg exploited 95%, random 5%)

    rtt_term  = (avg_rtt - bucket_min_rtt) / bucket_min_rtt * RTT_SCALE
                avg_rtt = tp->srtt_us >> 3
                capped at RTT_DEVIATION_CAP (8) x RTT_SCALE
                RTT_SCALE = 1_000_000

    rate_term = (bucket_max_rate - this_socket_rate) / bucket_max_rate
                * DELIVERY_SCALE
                DELIVERY_SCALE = 8_000_000

### Magnitude balance achieved (verified via remote_host_map dump)

    current leader   metric_value: 5,074,149
    second           metric_value: 5,263,802
    third            metric_value: 5,395,454
    (16 algorithms total; range 5.0M - 8.4M)

Top three separated by <250K. RTT term spread is ~1M. **RTT has a real chance
of flipping decisions at the top of the ranking** — the balanced design works.

### Diagnostic printk (ACTIVE in current builds)

Format in `tcp_conn_tuner.bpf.c` STATE_CB path:

    met alg=<idx> segs=<N> val=<V> rtt=<R> rate=<RT> smrtt=<S> bmrtt=<B> avgrtt=<A>

STRIP BEFORE RELEASE — doubles trace buffer pressure. Has caused LOST EVENTS at volume.
Keep running for 12-24h across all hosts, then decide strip vs keep.

### Observing

Start capture:

    sudo sh -c 'nohup cat /sys/kernel/tracing/trace_pipe > /tmp/met.log 2>&1 & echo "pid=$!"'

Buffer size (per CPU, non-persistent across reboot):

    echo 32768 | sudo tee /sys/kernel/tracing/buffer_size_kb

Only ONE reader at a time. To restart: `sudo pkill -f 'cat /sys/kernel/tracing/trace_pipe'` first.

Analyse per-algorithm queueing delay:

    sudo grep "met alg=" /tmp/met.log | awk '{
      alg=""; smrtt=""; avgrtt="";
      for(i=1;i<=NF;i++){
        if($i ~ /^alg=/){split($i,a,"="); alg=a[2]}
        if($i ~ /^smrtt=/){split($i,a,"="); smrtt=a[2]}
        if($i ~ /^avgrtt=/){split($i,a,"="); avgrtt=a[2]}
      }
      if(alg!="" && avgrtt!="" && smrtt!="") print alg, avgrtt-smrtt
    }' | awk '{sum[$1]+=$2; cnt[$1]++} END {for(a in sum) printf "alg=%s mean_qdelay=%d n=%d\n", a, sum[a]/cnt[a], cnt[a]}' | sort -k1.5 -n

Dump live running averages:

    sudo bpftool map dump name remote_host_map

### Open questions for next session

1. **Does the algorithm ordering generalize across hosts?** Only the heavy-traffic
   host has been analysed so far. If the same 3-4 algorithms cluster on all hosts,
   the signal is real. If each has a random ordering, it's path-specific noise.
2. **Does `bmrtt` stay realistic over 24h?** Bucket references are still monotonic
   (`min_rtt` only ever decreases; `max_rate_delivered` only ever increases). Reset on
   deploy, but drift over a day is unknown. If it plumbs back to unachievable lows,
   we have a third class of bug to fix (bucket reference decay).
3. **Does the summary converge?** After a day of 0.4.14 deciding, do we see 1-3
   algorithms with clear majorities, or still scattered?
4. **Final step:** strip diagnostic, cut 0.4.15, merge `diag/metric-terms` → `main`,
   release to all hosts, tag `0.4-15-custom`, GitHub release.

### Deploy sequence (the rm is REQUIRED)

    sudo systemctl stop bpftune
    sudo rm -f /var/lib/bpftune/tcp_conn_tuner.state      # delete BETWEEN stop and start
    sudo dpkg -i /mnt/backup/bpftune-custom-<version>-<arch>.deb
    sudo systemctl start bpftune

Without the `rm`, the state file restores old poisoned/immature bucket references into
the new binary and the fix silently does nothing. This bit us on the 0.4.12 deploy.

### Version sorting

0.4.14 sorts above the distro package (`0.0~git*`) because "4" > "0" at
the third character of the upstream part. **The `apt-mark hold` from prior sessions may
no longer be needed** — worth testing. The prior handoff said "hold required"; that was
true at `0-1` but 0.4.14 is authoritative.

### Host-switching workflow

Four hosts, two architectures, two roles each:

    amd64 builder       — source of truth, does the git push
    amd64 target        —
    aarch64 builder     — pulls, builds arm64, stages to shared mount
    aarch64 heavy-traffic host — where the tuner's choices matter most

I switch hosts myself — you don't need to tell me to SSH. Give me commands
for whichever host I'm on and label them clearly.

When "build both arches": you provide commit+push commands for the amd64 builder,
I switch manually to the aarch64 builder, you provide pull+build commands for that
host. Both .debs land in /mnt/backup/ shared mount. Install with the arch-appropriate
filename on each host.

---

## PRIOR STATE (earlier sessions before the metric work above)

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
illinois, yeah, lp, bic, highspeed, hybla, nv

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

## Build (see CURRENT STATE for current workflow)

ALWAYS `make clean` before dpkg-buildpackage (see "BUILD REQUIREMENT" below).
    git clone https://github.com/cddeppe/bpftune.git
    cd bpftune
    make clean
    dpkg-buildpackage -b -us -uc
Result: ../bpftune_<version>_<arch>.deb
Stage to: /mnt/backup/bpftune-custom-<version>-<arch>.deb

## Install

    sudo systemctl stop bpftune
    sudo rm -f /var/lib/bpftune/tcp_conn_tuner.state  # only when metric semantics change
    sudo dpkg -i /mnt/backup/bpftune-custom-<version>-<arch>.deb
    sudo systemctl start bpftune
    sleep 30   # let init finish before stopping, else no save

## Rollback
Restore /usr/sbin/bpftune.orig and /usr/lib/ARCH-linux-gnu/bpftune.orig/

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

## Known behavior: cdg dominance — SUPERSEDED

[This section was written before the SET-failure metric loop bug (#3
 below) was identified.  The apparent ~88% cdg selection was an
 artifact of that bug: cdg failed to set, its metric never updated,
 0 remained the minimum, and it kept winning.  The explanation about
 delay-based algorithms "structurally winning" was wrong.  See
 "Three upstream bugs fixed (0.4.5)" and "Why cdg was never the winner"
 below for the correct account.]
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
- 0.4-6 through 0.4-8-custom: cdg->nv in the algorithm list;
                 throughput term made meaningful (bytes/sec scaling);
                 RTT deviation cap (RTT_DEVIATION_CAP=8)
- 0.4-9-custom: dpkg version string fixed (0-1 -> 0.4.9) — permanently
                 resolves the apt-downgrade race against 0.0~git*;
                 adds diagnostic printk for metric analysis
- 0.4-10 through 0.4-14: metric overhaul.  See CURRENT STATE at top.
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

HISTORICAL FIX (pre-0.4.9): `sudo apt-mark hold bpftune libbpftune0` on each host.
Reinstall from /mnt/backup if already replaced.

CURRENT (0.4.9+): our dpkg version ("0.4.9", "0.4.14", ...) sorts ABOVE
the distro package ("0.0~git*"), so a plain `apt update && apt upgrade`
no longer overwrites the fork.  The apt-mark hold is likely no longer
needed but is still set on the fleet — test before removing.
