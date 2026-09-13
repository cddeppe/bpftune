# BPFTUNE FORK — HANDOFF

Custom fork of Oracle's bpftune. Adds 16 congestion-control algorithms,
per-destination learned state with persistence, and fixes for upstream
metric bugs. Goal: work on mixed-workload hosts (multiple path classes
per gateway), not just single-path datacenters.

## Repo & fleet

- Fork: https://github.com/cddeppe/bpftune  (active branch: `diag/metric-terms`)
- `main` untouched since 0.4.9 (tag `0.4-9-custom`). Everything since lives on the branch.
- Latest commit: `dffab80`
- Latest tag: `0.4-20-custom`

| Role | Arch | Version | Notes |
|------|------|---------|-------|
| Heavy-traffic (xray/YouTube) | aarch64 | **0.4.20** | capture → /tmp/met.log |
| Builder | amd64 | **0.4.20** | runs git push origin |
| Target | amd64 | **0.4.20** | |
| Builder | aarch64 | **0.4.20** | builds arm64 |
| shared mount: /mnt/backup/ holds .debs |

Verify: `dpkg-query -W -f='${Package} ${Version}\n' bpftune`

---

## SESSION 2026-09-13 (LATEST) — fixed-size metric sampling (0.4.20)
Version **0.4.20**, all four hosts. Tag `0.4-20-custom`.

Sockets cast their metric vote exactly once at 10000 segments
(METRIC_TRIGGER_SEGS in tcp_conn_tuner.h). Previous behavior was
close-only — a 200-segment health check and a 500,000-segment video
transfer weighted equally in bucket ranking.

RTT_CB falls through to shared metric-update at 10K; STATE_CB skips the
vote for sockets that already voted; closport printk moved before the
skip gate so close-event pairing survives. RETRANS_CB-promoted sockets
(forced BBR) abstain.

Verified on heavy host, 10-min YouTube stream: 25 midsamp thr=10000
fires, 23 met votes >= 10000, 33 close-path votes (< 10000). Archived
to /tmp/met.0.4.19.log, /tmp/met.0.4.20.log.

Open for 0.4.21: short closes still vote with equal weight (33 vs 23).
Options: (a) weight by segment count, (b) raise trigger, (c) require N
trigger votes per bucket. Prototype (a) and (c).

---

## SESSION 2026-09-13 (HISTORICAL) — mid-flight diagnostic (0.4.19)
Version **0.4.19**, heavy host only. Superseded by 0.4.20.

### What's live
Two diagnostics run concurrently:
- `met alg=N segs=X val=V rtt=R rate=RT smrtt=S bmrtt=B avgrtt=A` — close-time (STATE_CB).
- `midsamp port=P thr=T segs=N smin=M savg=A srate=R` — fires at 1K/5K/25K/100K/500K/1M segment crossings (RTT_CB).
- `closport port=P segs=N` — close-time tag for pairing.

### Problem being diagnosed
Close-only sampling misses long-lived sockets (video streams). The tuner learns
mostly from short control traffic. Need to know: is a fixed-size checkpoint
representative, and at what size does the metric converge?

### Confirmed finding
Queueing delay (`savg - smin`) grows with socket size:
- 1000-1500 segs → ~10-13K µs
- 5000-6800 segs → ~19-20K µs

Metric's RTT term measures "how big was the socket," not algorithm quality.
Fixed-size sampling is justified.

### Known limitation
`ops->local_port` is 443 for all home connections → midsamp and closport can't
pair. Next patch: add `ops->remote_port` to both.

### Next step
Let a YouTube stream run 10 minutes, then:
    sudo grep -c 'midsamp' /tmp/met.log
    sudo grep 'midsamp' /tmp/met.log | awk '{for(i=1;i<=NF;i++){if($i~/^thr=/){split($i,a,"="); print a[2]}}}' | sort -n | uniq -c
    sudo head -3 /tmp/met.log
- 25K+ checkpoints present → long sockets captured, design fixed-size sampling
- only 1K/5K present → long sockets missed, that's the next investigation
- LOST EVENTS in trace header → increase /sys/kernel/tracing/buffer_size_kb

---

## SESSION 2026-09-12 (HISTORY) — metric correctness overhaul
Versions **0.4.10 through 0.4.14**.

### Bugs fixed
1. Rate term was inert — `rate_delivered` computed as bytes/µs, truncated to 0 or 1
   for most connections. Throughput half contributed nothing. Fixed in 0.4.8 by
   scaling to bytes/sec — this was the initially silent fix that 0.4.14's diagnostic
   surfaced.
2. Small sockets (16-36 segments) were pinning the bucket's `min_rtt` to 144µs
   while real traffic saw ~30,000µs, so every real socket hit the RTT cap. Fixed
   in **0.4.12** with `METRIC_MIN_SEGS=100` filter.
3. Rate term was frozen per algorithm — `m->max_rate_delivered` (all-time best)
   was passed instead of the current socket's rate. Fixed in **0.4.13**.
4. RTT term was min-vs-min, path-constant (all 16 algorithms within ±5%). Fixed
   in **0.4.14** by switching to queueing delay: `avg_rtt - bucket_min_rtt`.

### Verified
- 0.4.13: heal mechanisms fire — `heal_rtt` 11, `heal_rate` 154 events/h.
- 0.4.14: RTT term produces algorithm-dependent values with ~1M spread vs
  <250K gaps between top-3 algorithms — the balanced design works.

---

## SESSION 2026-09-13 (continued) — 0.4.15 through 0.4.19

- **0.4.15** outlier rejection — `REF_OUTLIER_FACTOR 2`, reject reference
  updates >2× better than current. Stops cascading drops from one fast socket.
- **0.4.16** reference healing — when a socket is >`REF_HEAL_FACTOR`(3)× away
  from reference, drift it 1/`REF_HEAL_DIV`(16) of the gap. Both directions.
  Convergence verified in live trace.
- **0.4.17** heal-event diagnostics — `heal_rtt` / `heal_rate` printks placed in
  the BPF caller, not the shared header (bpf_printk doesn't exist in userspace).
- **0.4.18 — destination keying.** Bucket key changed from next-hop gateway to
  remote host IP (`ops->remote_ip4` / `ops->remote_ip6`). Map type `HASH` →
  `LRU_HASH`, size 1024 → 4096. Added `PERSIST_MIN_INSTANCES=8` filter.
  `STATE_VERSION` 1 → 2 (old gateway-keyed state is auto-rejected on load).
  **Effect: 2 buckets per host → 100+ per busy host, per-path decisions now real.**
- **0.4.19** mid-flight sampling diagnostic via `BPF_SOCK_OPS_RTT_CB` (this session's live work).

---

## Version history (compact)

- **0.4-2 to 0.4-5**: 16-algo expansion, persistence + `-x` reset, three upstream
  bug fixes (cdg metric loop, IPv4 no-gateway pollution, IPv6 fall-through).
- **0.4-6 to 0.4-8**: cdg→nv in algorithm list, throughput term meaningful,
  RTT_DEVIATION_CAP=8.
- **0.4-9**: dpkg version string changed from `0-1` to `0.4.9` — permanently
  resolves apt-downgrade race against distro's `0.0~git*`. `apt-mark hold` no
  longer strictly required.
- **0.4.10 → 0.4.14**: metric correctness (rate-live, threshold, queueing RTT).
  Diagnostics added incrementally.
- **0.4.15 → 0.4.19**: outlier rejection, reference healing, heal diagnostics,
  destination keying, mid-flight diagnostic.

---

## Bugs fixed (single list, dedup)

Upstream (unchanged code inherited from oracle/bpftune):
1. IPv4 no-gateway bucket pollution (`rt_gw4=0` collides with `::ffff:0.0.0.0`)
2. IPv6 no-gateway fall-through (`break` instead of `return` in switch)
3. Rate term bytes/µs truncation
4. Rate term passed alg-best instead of current socket's rate

Self-inflicted (from the 16-algorithm expansion we added):
5. cdg metric loop — kernel rejects cdg from `bpf_setsockopt`; the failure path
   wasn't checked, so cdg's metric stayed at 0 and won every comparison.

---

## Build / install / deploy

### Build
    cd /root/bpftune
    make clean                              # MANDATORY — stale objects bite
    dpkg-buildpackage -b -us -uc
    # produces ../bpftune_<version>_<arch>.deb

### Verify before install
    sudo rm -f /sys/fs/bpf/test_tcp_conn
    sudo bpftool prog load src/tcp_conn_tuner.bpf.o /sys/fs/bpf/test_tcp_conn
    echo exit=$?
    sudo rm -f /sys/fs/bpf/test_tcp_conn
Silent output + `exit=0` = verifier accepted.

### Deploy — state-file rule (READ CAREFULLY)
- **Metric-semantics change** (key type, reference semantics, version bump):
  DELETE the state file between stop and start. Example: 0.4.18 destination keying.
      sudo systemctl stop bpftune
      sudo rm -f /var/lib/bpftune/tcp_conn_tuner.state
      sudo dpkg -i <deb>; sudo systemctl start bpftune
- **Diagnostic-only or bugfix** (no semantic change): DO NOT delete state.
      sudo systemctl stop bpftune
      sudo dpkg -i <deb>; sudo systemctl start bpftune

### State file
`/var/lib/bpftune/tcp_conn_tuner.state` — 24B header (magic/version/sizes/count)
+ N×808B entries. Version checked on load; mismatch → ignored. Saved on
`fini()`, atomic write (tmp + rename).

### Trace capture
    sudo pkill -f 'cat /sys/kernel/tracing/trace_pipe'
    sudo mv /tmp/met.log /tmp/met.log.<prev>          # rotate
    sudo sh -c 'nohup cat /sys/kernel/tracing/trace_pipe > /tmp/met.log 2>&1 & echo pid=$!'
    echo 32768 | sudo tee /sys/kernel/tracing/buffer_size_kb
Only ONE reader at a time.

---

## Architecture

### Bucket key (as of 0.4.18)
`ops->remote_ip4` or `ops->remote_ip6` — the remote destination host.
Previously the next-hop gateway: 2 buckets per host, everything collapsed into
one IPv4 bucket. Now: 100+ buckets per busy host, each with its own RTT floor
and rate ceiling.

### Metric
    metric = rtt_term + rate_term          (lower wins)
    rtt_term  = (avg_rtt - bucket_min_rtt) / bucket_min_rtt * RTT_SCALE
                avg_rtt = tp->srtt_us >> 3 (µs), capped at
                RTT_DEVIATION_CAP(8) × RTT_SCALE
    rate_term = (bucket_max_rate - socket_rate) / bucket_max_rate * DELIVERY_SCALE
    RTT_SCALE = 1_000_000,  DELIVERY_SCALE = 8_000_000

### Reference update rules (0.4.15 + 0.4.16)
- New low `min_rtt`: accept unless more than `REF_OUTLIER_FACTOR`(2)× better
  than current.
- If `min_rtt` > `REF_HEAL_FACTOR`(3)× reference: drift up by 1/`REF_HEAL_DIV`(16)
  of the gap.
- Mirror rules for `max_rate` (reject huge spikes, heal downward).

### Persistence
Only buckets with `instances >= PERSIST_MIN_INSTANCES` (8) are written to state.
Recurring destinations survive; one-offs don't.

---

## Known kernel behavior

### cdg is permanently unusable from BPF
Kernel hardcodes rejection in `net/core/filter.c:sol_tcp_sockopt_congestion()`:
    /* "cdg" is the only cc that alloc a ptr in inet_csk_ca area.
     * The bpf-tcp-cc may overwrite this ptr after switching to cdg. */
    if (*optlen >= sizeof("cdg") - 1 && !strncmp("cdg", optval, *optlen))
        return -ENOTSUPP;
Not a fork bug. Handled by 0.4-5's `set_cong()` return-value check.

### Two summary blocks (per-netns, not a bug)
`bpftune -q summary` emits one CongAlg block per netns. Systemd sandboxes
(polkitd etc.) each get their own netns. `ip netns list` won't show them;
`lsns -t net` will.

### Multi-netns map dump
`bpftool map dump name remote_host_map` returns every map with that name across
every netns. `tools/bucket-leaders.py` flattens them — needs a map-ID column.

---

## Tools
`tools/bucket-leaders.py` — top-3 algorithms per bucket with sample counts.
Fetched on target hosts by SHA-pinned URL (branch name has a slash, so
`raw.githubusercontent.com/<branch>/...` doesn't work):
    sudo curl -sSLf https://raw.githubusercontent.com/cddeppe/bpftune/<SHA>/tools/bucket-leaders.py -o /usr/local/bin/bucket-leaders.py
    sudo chmod +x /usr/local/bin/bucket-leaders.py

## Open questions
1. Reference drift churn — home bucket `max_rate` moves 7M→47M across reads,
   leaders flip. Healing helps but doesn't stabilize busy buckets.
2. Fixed-size vs time-based trigger for long-socket sampling (informs this session).
3. Link-local filter — 169.254.x.x and fe80::/10 don't need CC tuning.
4. Branch merge to `main` + 0.4.20 release — everything since 0.4.9 lives on `diag/metric-terms`.
5. Add `remote_port` to `midsamp` / `closport` printks for reliable pairing.

## Working style (things that bite us)
- **User switches hosts manually.** Give commands for whichever host they're on and
  label clearly. Never tell them to SSH.
- **Heredocs with literal tabs mangle on paste.** Use `\t` escapes inside Python
  strings, or avoid tabs entirely.
- **Every patch script must assert before writing.** Anchor count, uniqueness,
  abort on mismatch. Half-applied edits are worse than clean failures.
- **Verify actual file content before editing.** `sed -n 'A,Bp' file | cat -A` first.
- **`make clean` before every build.** Non-negotiable.
- **SHA-pinned URLs** for target hosts without a repo clone.
