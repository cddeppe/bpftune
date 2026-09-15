# BPFTUNE FORK — HANDOFF

Custom fork of Oracle's bpftune. Adds 16 congestion-control algorithms,
per-destination learned state with persistence, and fixes for upstream
metric bugs. Goal: work on mixed-workload hosts (multiple path classes
per gateway), not just single-path datacenters.

## Repo & fleet

- Fork: https://github.com/cddeppe/bpftune  (active branch: `main`)
- `diag/metric-terms` is a stale pointer (fast-forwarded into `main`).
- Latest commit: `c3a27e3` (0.4.32)
- Latest release: 0.4.32

| Role | Arch | Version | Notes |
|------|------|---------|-------|
| Heavy-traffic (xray/YouTube) | aarch64 | **0.4.32** | capture -> /var/log/bpftune-met-YYYY-MM-DD.log |
| Builder | amd64 | **0.4.32** | runs git push origin |
| Target | amd64 | **0.4.32** | mostly idle |
| Builder | aarch64 | **0.4.32** | builds arm64 |
| shared mount: /mnt/backup/ holds .debs |

Verify: `dpkg-query -W -f='${Package} ${Version}\n' bpftune`




## SESSION 2026-09-15 — 0.4.32 reference-drift fix

Reference updates gated on sustained-transfer evidence; streak promotion
fixes up-lock.  All four hosts on 0.4.32; state file deleted on deploy
(STATE_VERSION 9 -> 10).

### The bug

Home bucket CLIENT-IP max_rate_delivered pinned at 1,683,564 (~1.68
Mbps).  Yesterday 15-33 Mbps.  Overnight, no streaming, dragged down.

Two failure modes, same root:

1. Down-drift.  Control-plane traffic (DNS ~1 segment, SSH keepalive ~2,
   ctrl-API ~50) delivers 88K-350K.  Its heal-down rule (rate < ref/3)
   pulled max_rate_delivered toward those rates.
2. Up-lock.  Once collapsed, real traffic at 5-10 Mbps sat above
   ref * 2 = 3.36 Mbps, so the outlier-rejection rule refused it.
   No population could raise the ref.

Consequence: rate_term = (max_rate_delivered - rate_delivered) /
max_rate_delivered, clamped to >= 0, was zero for every socket on that
bucket.  Leaderboards since the collapse were RTT-only.

### The fix (three parts, shipped together)

1. Gate.  Reference updates only apply when the socket demonstrated
   sustained transfer.  Segment-rung votes (>= 10K segments) pass
   naturally.  Time-check votes still fire at TIME_CHECK_MIN_SEGS (1000,
   ~24 KB/s) but the ref only moves at the new REF_TIME_CHECK_MIN_SEGS
   (2000, ~390 Kbps sustained).  Control sockets never reach 2000.
2. Streak promotion.  Consecutive readings above ref * 2 (rate) or below
   ref / 2 (rtt) accumulate.  At REF_HIGH_STREAK_N (5) in a row, promote
   the ref to the max (rate) or min (rtt) of the streak.  Single
   anomalous readings are still rejected.
3. Heal-down gated too.  Both heal-up (rtt) and heal-down (rate) only
   run when the gate is open.

struct remote_host gained rate_high_streak, rate_high_max, rtt_low_streak,
rtt_low_min.

### Design decisions left implicit by the brief

- STATE_CB (close-path) votes get allow_ref = false.  They are neither
  segment-rung nor time-check and cannot build a streak of 5 anyway.
- When allow_ref_update == false, the streak counters are frozen too.
  Otherwise a control-plane reading would reset a data socket's streak
  and "5 in a row" would rarely fire on mixed buckets.

### Verifier numbers (0.4.32)

- ESTABLISHED: 15,242 insns / 1,083 total / 224 peak.
- Vote: 2,982 insns / 188 total / 185 peak.

The vote program got SMALLER (was ~3,457 / ~234).  Verifier collapsed the
gated ref block to fewer states than the old parallel if/else chain.  Not
a regression.

### Verification on the heavy host (2026-09-15)

Deploy at 07:51 UTC, state deleted, all four hosts on 0.4.32.  One video,
bpftool map dump polled once per minute:

| t (UTC) | min_rtt | max_rate_delivered | instances |
|---------|---------|--------------------|-----------|
| 07:52   | 0       | 0                  | 15        |
| 07:53   | 28664   | 22.4M              | 27        |
| 07:54   | 28664   | 96.6M              | 27        |
| 07:56   | 28664   | 87.7M              | 32        |
| 07:57   | 28664   | 96.6M              | 35        |
| 07:58   | 28664   | 92.1M              | 36        |
| 08:00   | 28664   | 92.1M              | 41        |
| 08:01   | 28664   | 87.1M              | 45        |
| 08:02   | 28479   | 78.0M              | 47        |

Ref 0 -> 78M in 10 minutes.  +/-10% oscillation is the gated heal-down
path reacting to momentary low-momentum sockets.  instances grew 15 -> 47
with the ref stable: the gate filtered the small-socket traffic that used
to drag it.

bucket-leaders.py home bucket: dctcp=4.1M(n27) leads lp=6.6M(n1) and
scalable=6.8M(n3).  LEAD (x1.6).  Those values need a live rate_term;
RTT-only cannot produce them.

### Watch-list

- Does max_rate_delivered recover to 15-33M on subsequent quiet-then-busy
  cycles, or does it settle wherever the last streak left it?
- Streak threshold (5) too aggressive on bursty paths?  If ref
  over-promotes then heals down repeatedly, consider 7-10.
- REF_TIME_CHECK_MIN_SEGS (2000) lets too much legitimate low-bandwidth
  traffic move the ref?  If a 1M-long-lived chat stream holds ref low,
  raise it.


## SESSION 2026-09-14 — 0.4.29 continuous monitoring, branch cleanup

All four hosts on 0.4.29.  State file deleted on deploy (STATE_VERSION
7 -> 8).  Observation window restarts.

### What changed
- Time-based supplementary vote: every 60s, if segs advanced by
  >= 1000, run the vote + swap decision even without a segment
  checkpoint.  Long sockets (past 1M segments) now get evaluated
  continuously instead of never.
- Settle window 5s -> 60s.  Rate limiter, not a total cap.
- SWAP_MAX removed.  Sockets can swap as often as needed, at most once
  per minute.
- New second-best branch: when a socket is on best_i and best_i is
  worse than second_i by 1.25x for this socket, swap to second_i.
  Because second_v >= best_v, this threshold is naturally stricter than
  the normal case; it should fire rarely.

### Why
The observation earlier today caught a socket running bbr at 4.9M that
was swapped to veno and settled at 363K.  That confirmed the mechanism
can produce large improvements.  But three of four swaps produced no
post-swap vote -- they fired at 10K and the sockets closed before 25K.
The time-check exists so that long sockets which previously went
unmonitored past 1M segments get continuous attention.

### Branch state
main is now the working branch.  diag/metric-terms is a stale pointer at
the same commit.  Future commits go to main; pull with `git pull --rebase
origin main` on the builders.

### What to read after 24-72h
- Is the time-check firing?  Count met cookie lines -- should be
  noticeably higher than 0.4.28.
- Are swaps more frequent on long sockets?  `swap cookie` count should
  be higher, especially on sockets with segs > 1M.
- Are swaps improving sockets?  Compare swapped-socket metric drop
  against the ~8% natural drift on non-swapped sockets.
- Does the second-best branch fire at all?  If yes, is it firing on
  genuine "leader struggling" cases or producing thrash between the
  top two?

## SESSION 2026-09-14 — 0.4.28 loss term, forced-BBR removed

Tag pending.  All four hosts deployed with state reset.  Previous freeze
data (through 2026-09-14) was on the 2-term metric and is NOT comparable;
observation window restarts from 2026-09-14.

### What changed

1. Metric now has three terms: rtt_term + rate_term + loss_term.
   loss_term = min(retrans/segs_out, LOSS_CAP_BP=200 [2.00%]) / LOSS_CAP_BP
   * LOSS_SCALE (8M, same ceiling as the other two).  The met log line
   gains a loss= field.

2. Removed the hardcoded forced-BBR rescue in RETRANS_CB.  It switched
   any socket above ~1.56% loss to BBR and cleared its RTT_CB flag, so
   lossy sockets could never vote for any other algorithm — the metric
   couldn't learn which algorithm handles loss best.  With loss now in
   the metric, the tuner learns this.  Kept: the TCP_THIN_LINEAR_TIMEOUTS
   opt-in for lossy sockets.

3. STATE_VERSION 6 -> 7.  State file deleted on every host.
   Verifier: ESTABLISHED 15238 insns / 1083 states, vote 3055 / 209.

### Observation setup

- /var/log/bpftune-met-YYYY-MM-DD.log on heavy host: continuous met log
  capture (was /tmp/met.*.log previously; moved to /var/log so it
  survives reboots).
- Cron /etc/cron.d/bpftune-loss-hist (heavy host): 04:10 daily, writes
  a histogram of loss= values from the previous day to
  /var/log/bpftune-loss-hist-YYYY-MM-DD.txt.
- Existing daily leaderboard snapshot (04:05) and the 4-slice JSON dump
  (bpftune-tod cron, 00/06/12/18:05) continue.

### What to read after 24-48h

Histogram at /var/log/bpftune-loss-hist-DATE.txt:

- Mostly loss=0 → the loss term is inert on this traffic; its removal of
  forced-BBR was a cleanup, not a functional change.
- Spread from 0 to 8,000,000 → the term is doing work; some sockets
  penalized.
- Saturated at 8,000,000 → 2% cap is too low; raise LOSS_CAP_BP.

LOSS_CAP_BP and LOSS_SCALE are principled guesses, not derived from
data.  Tune after the distribution is observed.

### Question this session is answering

Does the removal of forced-BBR change which algorithms win on lossy
buckets?  If the leaderboards are identical to what they would have
been with forced-BBR, the change was neutral and the shortcut wasn't
actually costing anything.  If buckets that previously showed BBR
winning now show something else (or tie more), the shortcut was
biasing the results and we've learned something.

The freeze previously scheduled to end 2026-09-21 now effectively ends
2026-09-21 with the caveat that its first day's data is post-change.

## SESSION 2026-09-14 — 0.4.27 tracker re-anchor shipped

Tag `0.4-27-custom`, all four hosts active.  Freeze resumes until 2026-09-21.

### What 0.4.27 fixes

Bug found in 0.4.25/0.4.26: the incremental tracker in the vote path
only compared best_i against second_i on each vote.  A third algorithm
could drift into a lower position without ever being promoted through
the pair, so best_i ended up pointing at the #2 algorithm while the
top-3 output on the tool showed the true #1.  Observed on the fleet as
`best_i=cubic` disagreeing with a top-3 whose first entry was westwood.

Fix: at the end of the ESTABLISHED selection loop, persist that loop's
own minindex and metric_min into remote_host->best_i/best_v.  Both
values are already computed by the loop — no new variables, no verifier
cost.  State count went 1077 -> 1087.

### Why not the more obvious fixes

- Explicit min2index tracking: blew the 1M insn verifier budget.  State
  count 1077 -> 23392.  The verifier's state explosion is driven by the
  number of variables live across loop iterations, not instruction
  count.
- Userspace re-anchor via bpf_map_update_elem: designed but not built.
  Would require a periodic hook in the daemon; the tuner ops struct
  (struct bpftuner in include/bpftune/bpftune.h) has init/fini/
  event_handler/summarize slots but no periodic slot.  bpftune.c:490
  has the mainloop (bpftune_ring_buffer_poll, interval=100).  Adding a
  periodic slot is the cleaner long-term design but not necessary given
  the ESTABLISHED re-anchor fits.

### Verification

Heavy host after deploy: best_i and top-3 leader agree exactly on 6 of
7 buckets.  The seventh shows both at 4.4M on a one-decimal display —
either a genuine tie or a rounding ambiguity, not a real mismatch.

### State file note

0.4.27 did NOT require a state-file delete.  struct remote_host layout
is unchanged from 0.4.26; only additional writes go into existing
fields.

### The 0.4.26 swap behavioral data

Between 0.4.26 and 0.4.27, on the heavy host:
- 6 swaps fired in a 15-minute window
- All 6 targeted the current bucket leader (algo=8, reno)
- Zero cases of "swap a socket off the leader" — the downgrade-branch
  removal holds
- No thrash pattern

The question that remains open: does the swap *improve* the socket's
own performance afterward?  Cookie-tagged met lines make this
measurable.  Sample set is not yet large enough to say.

### Freeze window

2026-09-14 through 2026-09-21.  Daily at 04:00 a snapshot; 04:05 a
leaderboard dump to /var/log/bpftune-leaders/.  On 2026-09-21:

    ls -la /var/log/bpftune-leaders/
    for f in /var/log/bpftune-leaders/*.txt; do echo "== $f"; head -10 "$f"; done

Look for: home-IP leader stable by day 2-3; mid-traffic buckets trend
toward one leader; low-traffic tail neutral.  If high-traffic buckets
churn, first thing to look at is threshold values in
tcp_conn_tuner.h (SWAP_MARGIN_PCT, MIN_LEADER_TRUST).

### Watch-list for the freeze

- All buckets showed `TIGHT (x1.0)` — the metric's discrimination across
  algorithms on busy buckets is low.  Either the algorithms genuinely
  perform similarly on these paths, or the metric isn't picking up the
  differences.  Worth a direct look at rtt_term and rate_term separately
  across the 16 algorithms for one busy bucket.
- Orphan BPF programs and duplicate maps can accumulate across deploys
  (found two on ip-172-26-13-90, one on vps-3959, from pre-0.4.18 and
  ad-hoc starts).  Weekly check:
      sudo bpftool prog show 2>/dev/null | grep -cE 'name .*conn_tuner'
      sudo bpftool map show name remote_host_map 2>&1 | grep -c 'name'
  Expected: 2 programs, 1 map.  Anything higher means an orphan.

## SESSION 2026-09-14 — 0.4.25 and 0.4.26 mid-socket swap

Reconstructed 2026-09-14 from session memory; several intermediate
iterations are lost to a force-push cycle.  The key shipped changes and
the reasoning that produced them are captured below.

### 0.4.25 — mid-socket swap, live tracker

Goal: rescue a socket that drew a bad algorithm without waiting for it
to close.  A 30-minute video should not be stuck on a bad choice for
its entire life because exploration rolled the dice badly at
ESTABLISHED.

Design landed on:
- Trigger in RTT_CB when the socket's own metric sample (last_metric)
  is >= 1.25x the current best alternative (SWAP_MARGIN_PCT=125),
  sustained for two consecutive checkpoints (SWAP_BAD_BEFORE=2).
- Swap bounded by SWAP_MAX=2 and a 5-second settle window
  (T_SETTLE_NS) after any successful swap, to avoid thrashing on
  cold-start readings from tcp_reinit_congestion_control.
- Attribution: the swap executes via bpf_setsockopt(TCP_CONGESTION);
  subsequent checkpoints read statep->state and credit the new
  algorithm automatically.
- sk_storage_map value type changed __u64 -> struct conn_state carrying
  {state, swap_count, bad_checkpoints, settle_until, last_metric,
  pending_swap}.  STATE_VERSION 4 -> 5.

First implemented as three sockops programs (conn_tuner,
conn_tuner_vote, conn_tuner_swap) with a pending_swap flag passing
between vote and swap.  Split because the merged version blew the 1M
insn verifier budget: a 16-iteration loop adjacent to two live
map_value pointers plus helper calls causes state explosion.
Snapshotting best_alt_i at ESTABLISHED removed all loops from the vote
path.  Tags 0.4-25-custom.

### 0.4.26 — merge back, four fixes

Traces from 0.4.25 on the heavy host showed:
- 6 swaps in a 15-minute window targeting the current leader
- Zero downgrades (no socket moved off the leader)
- But: the target was `highspeed` on a socket whose bucket leader was
  `illinois` — best_alt_i was snapshot at ESTABLISHED and never
  refreshed.

Fixes in 0.4.26:

1. Merge vote+swap back to a single RTT_CB program.  Inline set_cong
   removes the pending_swap field and the cross-program queue; swaps
   fire in the same RTT_CB event.  Program count 3 -> 2.  Safe because
   neither program has loops after the snapshot removal.

2. METRIC_TRIGGER_SEGS back to 10000.  The 5K experiment biased the
   metric: every socket's 5K sample is roughly 10x lower than its 25K
   sample (no queueing yet, no delivery rate yet), so 5K samples
   dominated the leader ranking and cubic took over on n=1.

3. MIN_LEADER_TRUST=3.  A leader needs at least 3 votes before it can
   be a swap target.  Directly addresses the n=1 leader case.

4. No downgrade branch.  A socket already on best_i has no better
   target and is skipped.  Previously the code targeted second_i in
   that case — a strict downgrade that could thrash the socket between
   best and second-best.

5. SWAP_BAD_FIRST=1, SWAP_BAD_LATER=2.  First rescue fires on a single
   bad checkpoint (faster for 30s+ videos); subsequent swaps need two.

6. Live tracker: best_i/best_v/second_i/second_v on struct remote_host,
   refreshed on every vote via an incremental update.  Replaces the
   ESTABLISHED snapshot (best_alt_i removed from conn_state).

STATE_VERSION 5 -> 6.  Tags 0.4-26-custom.  Verified: 6 swaps in
15 minutes, all targeting the current leader with a 3.3x margin,
zero downgrades, no thrash.

## SESSION 2026-09-13 (FINAL) — 0.4.23 coverage fix, 0.4.24 long-socket voting

Fleet on 0.4.24.  Tags `0.4-23-custom`, `0.4-24-custom`.  Freeze in effect
until 2026-09-20 — no code changes to the tuner during that window.

### 0.4.23 — coverage counts selections, not votes

Bug found after 0.4.22 shipped: the coverage loop gated on per-alg
metric_count, which only increments when a socket votes.  Sockets that
never vote did not advance the coverage pointer, so the same algorithm
was assigned to every new connection until one of its sockets happened
to vote.  Observed on heavy host: cubic 149, htcp 99, bbr 84, indices
6-15 at zero.  Round-robin was not happening.

Fix: add __u64 selection_count to struct remote_host, increment on every
ESTABLISHED, force-pick (selection_count & 15) while selection_count <
32.  STATE_VERSION 3 -> 4.  Verified post-deploy: instances=102,
selection_count=32, vote distribution spread across all 16 algs.

### 0.4.24 — long-lived sockets vote more

A socket that survives to 1M segments now casts 5 votes (10K/25K/100K/
500K/1M) instead of 1.  Before this, a 30-min video and a 200 ms TLS
handshake contributed equally — wrong, since the long socket has been
tested against the network and the short one barely finished slow
start.  Below 10K: printk only.  METRIC_AVG_CAP=32 bounds a 5-vote
socket to ~15%% of an algo mean, so it cannot drown the pack.

Change: `if (next != METRIC_TRIGGER_SEGS)` -> `if (next <` in the RTT_CB
walker.  One-line fix.

### Freeze — 2026-09-13 through 2026-09-20

Crons running on all four hosts:
- 04:00 daily: stop/start bpftune (forces save_remote_host_map)
- 04:05 daily: bucket-leaders.py output to /var/log/bpftune-leaders/

On 2026-09-20, read the seven daily leaderboard files:

    ls -la /var/log/bpftune-leaders/

What to look for:
- Home IP (CLIENT-IP): leader should stabilize by day 2-3 and stay.
- Mid-traffic destinations: should trend toward a single leader.
- Low-traffic tail: neutral is correct, not a bug.

If high-traffic buckets churn across the week, the margin-gate
threshold (25%% lead requirement) is the first knob to tune.

### Do NOT during freeze

- Change the tuner without evidence from the freeze window.
- Re-derive the rl_update 25%%-step root cause (verified 0.4.21).
- Re-attempt margin gate as second loop (verifier budget).
- Reconstruct metric_value from /tmp/met.log (bmrtt drift).

---

## SESSION 2026-09-13 (FINAL 0.4.22 draft) — historical

Fleet on 0.4.22. Tag `0.4-22-custom`. GitHub release published.

### What 0.4.22 changed

- Margin-gated exploitation: if leader is not >=25%% ahead of 2nd,
  boost exploration from 1/20 to 1/4 until the winner is clear.
- Coverage lowered 5 -> 2 (bounded cost 32 sockets/bucket, not 80).
- Tie-break machinery removed to fit the 1M-instruction verifier
  limit.  Ties resolve by lowest index.

### Why coverage went back to 2

Coverage=5 needed 80 sockets per bucket.  On the fleet map the vast
majority of buckets have <20 connections total, so most destinations
would stay in permanent round-robin and never learn.  Coverage=2
establishes a first-pass distribution; the margin gate then decides
whether to commit.  Low-traffic buckets stay neutral by design —
that is the correct answer when there is not enough information.

### Freeze

No code changes to the tuner until at least 2026-09-20.  Observe:

- High-traffic buckets (home IP, active CDNs): leader should
  settle and stay settled.
- Mid-traffic buckets: should gradually prefer one algorithm.
- Low-traffic buckets: neutral is fine, that is the design.

If behavior is clearly wrong at the end of the window, the next
change has a well-defined target.  If it is right, work is done.

### Do NOT

- Change the tuner without evidence from the freeze window.
- Re-derive the 0.4.21 root cause (rl_update 25%% step, verified).
- Re-attempt margin gate as a second loop (verifier rejects).
- Reconstruct metric_value from /tmp/met.log (bmrtt drift).

---

## SESSION 2026-09-13 (LATEST) — 0.4.21 metric averaging + cold-start

Version **0.4.21**, heavy host only (fleet rollout pending). Tag pending.

### Root cause found

metric_value was updated via rl_update with BPFTUNE_BITSHIFT=2, i.e. a
25% step per observation.  Effective memory ~4 samples regardless of
metric_count.  Home-bucket leader churn on CLIENT-IP (bbr 5.8M ->
7.59M over 5 votes) was this step, not path drift.  Verified by -r 0
drop-in: step became 1/64, matching the exact delta seen in the map.

### Fixes shipped on 0.4.21

1. metric_value -> count-based incremental mean, divisor min(count+1,
   METRIC_AVG_CAP=32).  Early observations move the value a lot, later
   ones settle it.  Verified: bbr 5->6 moved by (obs-value)/6 exactly.
2. Skip 127.0.0.0/8, 169.254.0.0/16, ::1, fe80::/10 at bucket-key.
3. Add remote_port to midsamp and closport printks (fixes 443 pairing).
4. Remove dead per-alg min_rtt/max_rate_delivered; STATE_VERSION=3.
5. Cold-start coverage: force-sample least-sampled alg until every alg
   has >= 5 votes, then normal epsilon-greedy.  Bounded (<=80 sockets).

### What we learned about cold-start

Coverage=2 was tried first.  Result: illinois raced to n=25 while nv
held the best log mean but was stuck at n=6.  Greedy exits coverage at
n=2, where the mean is mostly noise, so it commits to whichever alg
sampled lucky first.  Coverage=5 keeps the distribution flat (observed: 16
algs all between 4 and 7 after 30 min), then greedy resumes.

Margin-gated exploitation (raise epsilon to 1/4 while leader is not
>=20% ahead of 2nd) was attempted and REJECTED by the verifier: the
second 16-iteration loop pushed the program over the 1,000,000-
instruction limit.  Not viable without restructuring.

### Verification method (important)

Do NOT reconstruct metric_value from /tmp/met.log.  Bucket min_rtt
drifts mid-run so bmrtt filters miss votes, and trace_pipe drops
events under load.  Correct method: take two map dumps minutes
apart and solve V2 = V1 + (obs - V1)/(N+1) for each alg that gained
a vote.

### 0.4.22 scope (from tonight)

- Near-tie leader rotation.  When top algs are within ~12%, greedy
  oscillates between them without converging.  Margin gate would fix
  it but needs a non-loop implementation or verifier headroom.
- Vote weighting: long-lived sockets vote once then go silent; a
  30-min video and a 200ms handshake both contribute one vote.
  Options: re-vote at 100K/1M checkpoints, or weight by bytes.
- Tool: multi-netns column, n>=3 verdict gate, group no-closes rows.
- RFC1918 / VPC gateway filter (172.16/12 etc) not yet applied.

---

## SESSION 2026-09-13 (HISTORICAL) — fixed-size metric sampling (0.4.20)
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
- **0.4-20**: fixed-size metric sampling.  Sockets vote once at 10K segments
  instead of only at close.
- **0.4-21**: `metric_value` → count-based incremental mean (replaces the
  25%%-step `rl_update` that caused leader churn).  Cold-start coverage.
  Pollution filter (127/8, 169.254/16, fe80::/10).  `remote_port` on midsamp
  and closport.
- **0.4-22**: margin-gated exploitation; coverage 5→2; tie-break switch
  dropped to fit the verifier.
- **0.4-23**: coverage counts SELECTIONS, not votes.  Fixes a bug where the
  same algorithm was assigned until one of its sockets happened to vote.
- **0.4-24**: vote at every checkpoint (10K..1M).  Long-lived sockets now
  carry more weight than short ones.
- **0.4-25**: mid-socket swap.  Trigger moved from rank-at-assign to live
  socket metric vs live bucket leader.  Live `best_i`/`second_i` on remote_host.
- **0.4-26**: merged vote+swap back to one RTT_CB program (3→2).  `MIN_LEADER_TRUST=3`;
  no downgrade branch; `SWAP_BAD_FIRST=1`, `SWAP_BAD_LATER=2`.

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
   Note: the unchecked-return pattern is inherited from upstream, but the
   manifestation is ours — upstream ships 4 algorithms that all set
   successfully on this kernel, so the bug could not fire there.  It became
   real the moment we added cdg.

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
4. ~~Branch merge to `main` + 0.4.20 release~~ — done.
5. ~~Add `remote_port` to `midsamp` / `closport`~~ — done in 0.4.21.
6. Does the mid-socket swap actually help?  Six swaps on 2026-09-14 targeted
   the current bucket leader with a clean margin; no thrash observed.
   Post-swap improvement is committed to the log (cookie= on met lines) but
   not yet quantified across many sockets.
7. Short-video rescue — sub-15s videos cannot accumulate the checkpoints
   needed before close.  Physics limit, not a design gap.

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
