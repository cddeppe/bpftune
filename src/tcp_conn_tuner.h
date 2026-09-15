/* SPDX-License-Identifier: GPL-2.0 WITH Linux-syscall-note */
/*
 * Copyright (c) 2023, Oracle and/or its affiliates.
 *
 * This program is free software; you can redistribute it and/or
 * modify it under the terms of the GNU General Public
 * License v2 as published by the Free Software Foundation.
 *
 * This program is distributed in the hope that it will be useful,
 * but WITHOUT ANY WARRANTY; without even the implied warranty of
 * MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the GNU
 * General Public License for more details.
 *
 * You should have received a copy of the GNU General Public
 * License along with this program.  If not, see <https://www.gnu.org/licenses/>.
 */

#include <bpftune/bpftune.h>

enum tcp_cong_tunables {
    TCP_CONG,
    TCP_ALLOWED_CONG,
    TCP_AVAILABLE_CONG,
    TCP_CONG_DEFAULT,
    TCP_THIN_LINEAR_TIMEOUTS
};

enum tcp_cong_scenarios {
    TCP_CONG_SET,
};

#define CONG_MAXNAME 16

#define CONN_TUNER_BPF "bpftune_conn_tuner"
#define CONN_TUNER_VOTE_BPF "bpftune_conn_tuner_vote"

/* Expanded to 16 algorithms (must be a power of two for bitmask logic) */
enum tcp_states {
    TCP_STATE_CONG_CUBIC,
    TCP_STATE_CONG_BBR,
    TCP_STATE_CONG_HTCP,
    TCP_STATE_CONG_DCTCP,
    TCP_STATE_CONG_SCALABLE,
    TCP_STATE_CONG_VEGAS,
    TCP_STATE_CONG_VENO,
    TCP_STATE_CONG_WESTWOOD,
    TCP_STATE_CONG_RENO,
    TCP_STATE_CONG_ILLINOIS,
    TCP_STATE_CONG_YEAH,
    TCP_STATE_CONG_LP,
    TCP_STATE_CONG_BIC,
    TCP_STATE_CONG_HIGHSPEED,
    TCP_STATE_CONG_HYBLA,
    TCP_STATE_CONG_NV,
    NUM_TCP_CONG_ALGS
};

/* match order of enum tcp_states */
const char congs[NUM_TCP_CONG_ALGS][CONG_MAXNAME] = {
    "cubic", "bbr", "htcp", "dctcp",
    "scalable", "vegas", "veno", "westwood",
    "reno", "illinois", "yeah", "lp",
    "bic", "highspeed", "hybla", "nv"
};

struct conn_state {
    __u64 state;
    __u64 swap_count;
    __u64 bad_checkpoints;
    __u64 settle_until;
    __u64 last_metric;       /* most recent metric sample; 0 = unset */
    __u64 last_time_check;   /* ns of last 60s-cadence vote */
    __u64 time_check_segs;   /* segments at last time check */
    __u64 best_seen_metric;  /* lowest metric seen on this socket */
    __u64 best_seen_alg;     /* algorithm it was running then */
    __u64 frozen;            /* 1 = no more swaps on this socket */
};

struct tcp_conn_metric {
    __u64 state_flags;
    __u64 greedy_count;
    __u64 metric_count;
    __u64 metric_value;
};

#define NUM_TCP_CONN_METRICS NUM_TCP_CONG_ALGS

struct tcp_conn_event_data {
    struct in6_addr raddr;
    __u64 state_flags;
    __u64 rate_delivered;
    __u64 min_rtt;
    __u64 metric;
};

struct remote_host {
    __u64 min_rtt;
    __u64 max_rate_delivered;
    __u64 instances;
    __u64 selection_count;
    /* Incremental best and 2nd-best across metrics; refreshed on every
     * vote.  best_v == 0 means unset.  Vote path reads these for a
     * live swap target without a 16-iter loop. */
    __u64 best_i;
    __u64 best_v;
    __u64 second_i;
    __u64 second_v;
    /* Reference-refresh streak state.  A single far-better-than-
     * reference reading is treated as an outlier; REF_HIGH_STREAK_N
     * in a row promote the reference to the best of the streak.
     * Frozen while allow_ref_update is false (control-plane votes). */
    __u64 rate_high_streak;   /* consecutive readings > ref * REF_OUTLIER_FACTOR */
    __u64 rate_high_max;      /* max of those readings */
    __u64 rtt_low_streak;     /* consecutive readings < ref / REF_OUTLIER_FACTOR */
    __u64 rtt_low_min;        /* min of those readings */
    struct tcp_conn_metric metrics[NUM_TCP_CONN_METRICS];
};

#define REMOTE_HOST_MIN_INSTANCES 4
#define DROP_SHIFT 6
#define RTT_SCALE 1000000
/* cap the RTT deviation term so one anomalously-low
 * measurement cannot permanently poison the metric. */
#define RTT_DEVIATION_CAP 8
/* Match the throughput term's ceiling to the RTT term's (which is
 * RTT_SCALE * RTT_DEVIATION_CAP = 8000000).  Without this, RTT could
 * contribute up to 8x more than throughput to the summed metric,
 * making the throughput term effectively decorative even though it's
 * now producing real values. */
#define DELIVERY_SCALE 8000000
/* Loss term: penalizes retransmits as a fraction of our sent
 * segments.  LOSS_CAP_BP is the loss rate in basis points that
 * earns the full penalty; above it the term saturates.  200 = 2%. */
#define LOSS_CAP_BP   200
#define LOSS_SCALE    8000000
#define METRIC_MIN_SEGS 100
#define METRIC_TRIGGER_SEGS 10000
#define METRIC_AVG_CAP 32
/* Mid-socket swap policy.  Compare the current algorithm bucket EMA
 * against the best alternative bucket EMA - not a single socket
 * sample against an average.  If the ratio holds for SWAP_AFTER_BAD
 * consecutive checkpoints and the socket has not already swapped
 * MAX_SWAPS times, move it.  After a swap, suppress re-judgement
 * for T_SETTLE_NS: tcp_reinit_congestion_control resets cwnd and
 * ssthresh, so early samples on the new algorithm are a cold start. */
#define SWAP_MARGIN_PCT 125     /* last_metric >= best_alt * 125 / 100 fires */
#define SWAP_BAD_FIRST      2   /* checkpoints before first swap */
#define SWAP_BAD_LATER 2   /* checkpoints before subsequent swaps */
/* After this many swaps, freeze: go to the algorithm on which
 * this socket performed best, and stop trying.  A socket that
 * has cycled through this many algorithms and stayed bad has a
 * path or app problem that no algorithm can fix. */
#define FREEZE_AFTER_SWAPS 4
#define SWAP_BAD_DESPERATE_PCT 200  /* 2.0x leader -> immediate fire */
#define MIN_LEADER_TRUST 3 /* min votes before targeting a leader */
#define SWAP_MAX       2
#define T_SETTLE_NS      (60ULL * 1000000000ULL)
#define T_TIME_CHECK_NS  (60ULL * 1000000000ULL)  /* supplementary vote cadence */
#define TIME_CHECK_MIN_SEGS 1000  /* require progress between checks */

/* Minimum instances before a bucket is written to the persistent
 * state file.  One-off destinations never accumulate enough samples
 * to be worth persisting; recurring paths (CDN, tunnel) do. */
#define PERSIST_MIN_INSTANCES 8
/* Reject reference updates that are more than this factor better
 * than the current reference (lower for min_rtt, higher for
 * max_rate).  A single unusually-fast or slow socket must not be
 * able to permanently anchor the reference to a value the rest of
 * the traffic cannot reach.  Progressive improvements (each step
 * within the factor) still apply. */
#define REF_OUTLIER_FACTOR 2
/* Healing: raise the floor / lower the ceiling toward observed
 * values when traffic consistently disagrees with the reference.
 * Guards against a poisoned reference that cannot self-correct.
 * A socket must be REF_HEAL_FACTOR away from the reference to
 * trigger; each triggering socket moves 1/REF_HEAL_DIV of the gap.
 * Healthy traffic (ratio ~1) never triggers this. */
#define REF_HEAL_FACTOR 3
#define REF_HEAL_DIV 16

/* Reference-update gate.  Segment-rung votes (>= 10K segments)
 * demonstrate sustained throughput and pass naturally.  Time-check
 * votes still fire at TIME_CHECK_MIN_SEGS, but may only move the
 * references when the socket has advanced by REF_TIME_CHECK_MIN_SEGS
 * (~390 Kbps sustained).  Control-plane traffic (DNS, keepalive,
 * ctrl-API) never comes close, so it cannot drift the references in
 * either direction. */
#define REF_TIME_CHECK_MIN_SEGS  2000
/* Consecutive far-better-than-reference readings that promote the
 * reference.  One anomalous reading is still rejected;
 * REF_HIGH_STREAK_N in a row are treated as evidence and promote the
 * reference to the max of the rate streak / min of the rtt streak. */
#define REF_HIGH_STREAK_N        5

/* The metric we calcuate compares current connection min_rtt and rate_delivered to
 * the min rtt and max rate delivered we have observed for the remote host.
 * The idea is that we want to reward congestion control algorithms that minimize
 * RTT and maximize delivery rate, as these are operating at the bottleneck
 * bandwitdh, which is the optimal operating mode.  This does not unduly favour
 * a particular algorithm in practice it seems, and choices can fluctuate over
 * time.  One concern is that the delivery rate is rather low and does not
 * fluctuate much - we see 1 most often for delivery rate.  Our cost function
 * rates rtt deviation and delivery rate deviation equally however; this may
 * need to be tweaked.
 *
 * Cost function is
 *
 * (conn_min_rtt - min_rtt)        +  (max_delivery_rate - delivery_rate)
 *  -----------------------           -----------------------------------
 *  overall min rtt                    overall_max_delivery_rate
 *
 *
 * Both of these are scaled by RTT_SCALE, DELIVERY_SCALE to ensure we get integer
 * values.  Note we do not need to square values because both are asymmetric;
 * a connection min_rtt > overall_min_rtt is bad, while a delivery_rate < overall
 * max delivery rate is bad.  As a result a higher cost here is a problem, and
 * we pick action (congestion algorithm) with minimum cost.
 *
 * Metrics are updated using standard reinforcement learning update;
 *
 * new_estimate = old_estimate + learning_rate * (reward - old_estimate)
 */
static __always_inline __u64 tcp_metric_calc(struct remote_host *r,
                                             __u64 min_rtt,
                                             __u64 avg_rtt,
                                             __u64 rate_delivered,
                                             bool allow_ref_update,
                                             __u64 *rtt_term_out,
                                             __u64 *rate_term_out,
                                             __u64 *heal_rtt_out,
                                             __u64 *heal_rate_out)
{
        __u64 metric = 0;
        __u64 rtt_term = 0;
        __u64 rate_term = 0;
        __u64 heal_rtt = 0;
        __u64 heal_rate = 0;

        if (allow_ref_update) {
                /* min_rtt: single far-low reading is an outlier;
                 * REF_HIGH_STREAK_N in a row promote to the min of
                 * the streak.  Progressive improvements (below ref
                 * but within REF_OUTLIER_FACTOR) accepted directly. */
                if (!r->min_rtt) {
                        r->min_rtt = min_rtt;
                        r->rtt_low_streak = 0;
                        r->rtt_low_min = 0;
                } else if (min_rtt < r->min_rtt / REF_OUTLIER_FACTOR) {
                        r->rtt_low_streak++;
                        if (r->rtt_low_min == 0 || min_rtt < r->rtt_low_min)
                                r->rtt_low_min = min_rtt;
                        if (r->rtt_low_streak >= REF_HIGH_STREAK_N) {
                                r->min_rtt = r->rtt_low_min;
                                r->rtt_low_streak = 0;
                                r->rtt_low_min = 0;
                        }
                } else if (min_rtt < r->min_rtt) {
                        r->min_rtt = min_rtt;
                        r->rtt_low_streak = 0;
                        r->rtt_low_min = 0;
                } else {
                        r->rtt_low_streak = 0;
                        r->rtt_low_min = 0;
                        if (min_rtt > r->min_rtt * REF_HEAL_FACTOR) {
                                r->min_rtt += (min_rtt - r->min_rtt) / REF_HEAL_DIV;
                                heal_rtt = r->min_rtt;
                        }
                }

                /* max_rate_delivered: symmetric. */
                if (!r->max_rate_delivered) {
                        r->max_rate_delivered = rate_delivered;
                        r->rate_high_streak = 0;
                        r->rate_high_max = 0;
                } else if (rate_delivered > r->max_rate_delivered * REF_OUTLIER_FACTOR) {
                        r->rate_high_streak++;
                        if (rate_delivered > r->rate_high_max)
                                r->rate_high_max = rate_delivered;
                        if (r->rate_high_streak >= REF_HIGH_STREAK_N) {
                                r->max_rate_delivered = r->rate_high_max;
                                r->rate_high_streak = 0;
                                r->rate_high_max = 0;
                        }
                } else if (rate_delivered > r->max_rate_delivered) {
                        r->max_rate_delivered = rate_delivered;
                        r->rate_high_streak = 0;
                        r->rate_high_max = 0;
                } else {
                        r->rate_high_streak = 0;
                        r->rate_high_max = 0;
                        if (rate_delivered * REF_HEAL_FACTOR < r->max_rate_delivered) {
                                r->max_rate_delivered -=
                                        (r->max_rate_delivered - rate_delivered) / REF_HEAL_DIV;
                                heal_rate = r->max_rate_delivered;
                        }
                }
        }
        if (r->min_rtt) {
                __u64 dev = avg_rtt > r->min_rtt ? avg_rtt - r->min_rtt : 0;
                __u64 cap = (__u64)r->min_rtt * RTT_DEVIATION_CAP;
                if (dev > cap)
                        dev = cap;
                rtt_term = (dev * RTT_SCALE) / r->min_rtt;
                metric += rtt_term;
        }
        if (r->max_rate_delivered) {
                /* Guard against negative: if this socket beat the
                 * reference without updating it (outlier rejection),
                 * treat it as perfect rather than giving a bonus. */
                if (rate_delivered < r->max_rate_delivered)
                        rate_term = ((r->max_rate_delivered - rate_delivered)
                                     * DELIVERY_SCALE) / r->max_rate_delivered;
                metric += rate_term;
        }
        if (rtt_term_out)
                *rtt_term_out = rtt_term;
        if (rate_term_out)
                *rate_term_out = rate_term;
        if (heal_rtt_out)
                *heal_rtt_out = heal_rtt;
        if (heal_rate_out)
                *heal_rate_out = heal_rate;
        return metric;
}
