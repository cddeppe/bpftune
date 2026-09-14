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

#include <bpftune/bpftune.bpf.h>

#include "tcp_conn_tuner.h"

#define TCP_THIN_LINEAR_TIMEOUTS	16

__u64 tcp_cong_choices[NUM_TCP_CONG_ALGS];

long long tcp_thin_lto = 0;

BPF_MAP_DEF(remote_host_map, BPF_MAP_TYPE_LRU_HASH, struct in6_addr, struct remote_host, 4096, 0);

BPF_MAP_DEF(sk_storage_map, BPF_MAP_TYPE_SK_STORAGE, int, struct conn_state, 0, BPF_F_NO_PREALLOC);

BPF_MAP_DEF(midsamp_map, BPF_MAP_TYPE_SK_STORAGE, int, __u64, 0, BPF_F_NO_PREALLOC);

/* per-CPU scratch buffer used to initialize new remote_host entries without
 * placing a >512B struct temporary on the BPF stack (NUM_TCP_CONG_ALGS=16
 * makes struct remote_host 792 bytes, exceeding the 512-byte stack limit).
 */
BPF_MAP_DEF(remote_host_scratch, BPF_MAP_TYPE_PERCPU_ARRAY, __u32,
	    struct remote_host, 1, 0);

/* if we have not looked up the host >= REMOTE_HOST_MIN_INSTANCES, return NULL.
 * This ensures we only apply RL to hosts with which we have multiple
 * interactions.
 */
static __always_inline struct remote_host *get_remote_host(struct in6_addr *key,
							   bool initial)
{
	__u32 zero = 0;
	struct remote_host *remote_host;
	struct remote_host *scratch;

	remote_host = bpf_map_lookup_elem(&remote_host_map, key);
	if (remote_host) {
		if (initial)
			remote_host->instances++;
	} else {
		/* Use per-CPU scratch to avoid a >512B stack temporary.
		 * bpf_map_update_elem copies sizeof(*scratch) bytes from
		 * the kernel-memory scratch buffer into the hash map.
		 */
		scratch = bpf_map_lookup_elem(&remote_host_scratch, &zero);
		if (!scratch)
			return NULL;
		__builtin_memset(scratch, 0, sizeof(*scratch));
		scratch->instances = 1;
		bpf_map_update_elem(&remote_host_map, key, scratch, BPF_ANY);
		return NULL;
	}
	if (remote_host->instances < REMOTE_HOST_MIN_INSTANCES)
		return NULL;
	return remote_host;
}

static __always_inline int set_cong(struct bpf_sock_ops *ops, __u8 i)
{
        int ret;

        ret = bpf_setsockopt(ops, SOL_TCP, TCP_CONGESTION, (void *)congs[i],
                             sizeof(congs[i]));
        if (ret)
                return ret;
        tcp_cong_choices[i & (NUM_TCP_CONG_ALGS - 1)]++;
        /* update state */
        struct bpf_sock *sk = ops->sk;
        struct conn_state *statep;

        if (!sk)
                return 0;
        statep = bpf_sk_storage_get(&sk_storage_map, sk, 0,
                                    BPF_SK_STORAGE_GET_F_CREATE);
        if (statep) {
                statep->state = (__u64)i;
                statep->bad_checkpoints = 0;
        }
        return 0;
}

__u64 tcp_thin_lto_choices;

SEC("sockops")
int bpftune_conn_tuner(struct bpf_sock_ops *ops)
{
    int cb_flags = BPF_SOCK_OPS_STATE_CB_FLAG|BPF_SOCK_OPS_RETRANS_CB_FLAG|BPF_SOCK_OPS_RTT_CB_FLAG;
    struct remote_host *remote_host;
    struct in6_addr raddr = {};
    struct in6_addr *key = &raddr;
    struct bpf_sock *sk = ops->sk;
    struct conn_state *statep = NULL;

    switch (ops->op) {
    case BPF_SOCK_OPS_ACTIVE_ESTABLISHED_CB:
    case BPF_SOCK_OPS_PASSIVE_ESTABLISHED_CB:
        bpf_sock_ops_cb_flags_set(ops, cb_flags);
        break;
    case BPF_SOCK_OPS_RETRANS_CB:
        if (ops->total_retrans > (ops->segs_out >> DROP_SHIFT)) {
            if (sk)
                statep = bpf_sk_storage_get(&sk_storage_map, sk, 0, 0);
            if (!statep || statep->state != TCP_STATE_CONG_BBR) {
                if (!statep && !tcp_thin_lto) {
                    int one = 1;
                    if (!bpf_setsockopt(ops, SOL_TCP, TCP_THIN_LINEAR_TIMEOUTS,
                                        &one, sizeof(one)))
                        tcp_thin_lto_choices++;
                }
                (void)set_cong(ops, TCP_STATE_CONG_BBR);
                bpf_sock_ops_cb_flags_set(ops, BPF_SOCK_OPS_STATE_CB_FLAG);
            }
        }
        return 1;
    default:
        return 1;
    }

    if (!sk)
        return 1;
    if (ops->family == AF_INET) {
        __u32 ip4 = bpf_ntohl(ops->remote_ip4);
        if ((ip4 & 0xff000000) == 0x7f000000) return 1;
        if ((ip4 & 0xffff0000) == 0xa9fe0000) return 1;
    } else if (ops->family == AF_INET6) {
        if (ops->remote_ip6[0] == 0 && ops->remote_ip6[1] == 0 && ops->remote_ip6[2] == 0 && ops->remote_ip6[3] == bpf_htonl(1)) return 1;
        if ((ops->remote_ip6[0] & bpf_htonl(0xffc00000)) == bpf_htonl(0xfe800000)) return 1;
    }
    switch (ops->family) {
    case AF_INET:
        key->s6_addr32[2] = bpf_htonl(0xffff);
        key->s6_addr32[3] = ops->remote_ip4;
        break;
    case AF_INET6:
        key->s6_addr32[0] = ops->remote_ip6[0];
        key->s6_addr32[1] = ops->remote_ip6[1];
        key->s6_addr32[2] = ops->remote_ip6[2];
        key->s6_addr32[3] = ops->remote_ip6[3];
        break;
    default:
        return 1;
    }
    remote_host = get_remote_host(key, true);
    if (!remote_host)
        return 1;

    {
        __u64 metric_min = ~((__u64)0x0);
        __u8 i, minindex = 0, s;

        if (remote_host->selection_count < 2 * NUM_TCP_CONN_METRICS) {
            __u8 forced = remote_host->selection_count & (NUM_TCP_CONN_METRICS - 1);
            remote_host->selection_count++;
            if (set_cong(ops, forced))
                remote_host->metrics[forced].metric_value = ~((__u64)0);
            return 1;
        }

        {
            __u64 min2 = ~((__u64)0);
            __u8 min2index = 0;
            for (i = 0; i < NUM_TCP_CONN_METRICS; i++) {
                __u64 v = remote_host->metrics[i].metric_value;
                if (v < metric_min) {
                    min2 = metric_min;
                    min2index = minindex;
                    metric_min = v;
                    minindex = i;
                } else if (v < min2) {
                    min2 = v;
                    min2index = i;
                }
            }
            minindex &= (NUM_TCP_CONN_METRICS - 1);
            if (min2 == ~((__u64)0) || metric_min * 5 > min2 * 4)
                s = epsilon_greedy(minindex, NUM_TCP_CONN_METRICS, 4);
            else
                s = epsilon_greedy(minindex, NUM_TCP_CONN_METRICS, 20);

            s &= (NUM_TCP_CONG_ALGS - 1);
            if (set_cong(ops, s))
                remote_host->metrics[s].metric_value = ~((__u64)0);

            /* Snapshot the best alternative for this socket.  If greedy
             * picked the best algo (minindex), the alternative is the
             * second-best (min2index); otherwise (exploration picked a
             * non-best algo), the alternative is minindex itself.  RTT_CB
             * reads this snapshot rather than rescanning 16 slots on every
             * checkpoint (that rescan is what blew the verifier budget). */
            {
                struct conn_state *csp = bpf_sk_storage_get(&sk_storage_map, sk, 0, 0);
                if (csp) {
                    if (s == minindex)
                        csp->best_alt_i = (min2 == ~((__u64)0)) ? ~((__u64)0) : (__u64)min2index;
                    else
                        csp->best_alt_i = (__u64)minindex;
                }
            }
        }
    }
    return 1;
}

SEC("sockops")
int bpftune_conn_tuner_vote(struct bpf_sock_ops *ops)
{
    struct remote_host *remote_host;
    struct in6_addr raddr = {};
    struct in6_addr *key = &raddr;
    struct bpf_sock *sk = ops->sk;
    struct tcp_sock *tp = NULL;
    struct conn_state *statep = NULL;
    __u64 *nextp;
    __u64 segs, next;
    struct tcp_sock *tps;
    __u64 smin, savg, srate, srate_raw, smss, sinter;
    __u64 metric, min_rtt, avg_rtt, rate_interval_us, rate_delivered, mss;
    struct tcp_conn_metric *m;
    bool greedy = true;
    __u8 s;
    bool is_close;
    __u64 now;

    switch (ops->op) {
    case BPF_SOCK_OPS_RTT_CB:
        if (!sk)
            return 1;
        tps = bpf_skc_to_tcp_sock(sk);
        if (!tps)
            return 1;
        nextp = bpf_sk_storage_get(&midsamp_map, sk, 0,
                                   BPF_SK_STORAGE_GET_F_CREATE);
        if (!nextp)
            return 1;
        next = *nextp;
        if (!next)
            next = 1000;
        segs = (__u64)ops->segs_out + (__u64)ops->segs_in;
        if (segs < next)
            return 1;
        smin = (__u64)tps->rtt_min.s[0].v;
        savg = (__u64)(tps->srtt_us >> 3);
        sinter = (__u64)tps->rate_interval_us;
        srate_raw = (__u64)tps->rate_delivered;
        smss = (__u64)tps->mss_cache;
        srate = sinter ? (srate_raw * smss * 1000000ULL) / sinter : 0;
        bpf_printk("midsamp port=%u rport=%u thr=%llu segs=%llu smin=%llu savg=%llu srate=%llu",
                   ops->local_port, bpf_ntohl(ops->remote_port), next, segs, smin, savg, srate);
        switch (next) {
        case 1000:   *nextp = 5000;    break;
        case 5000:   *nextp = 10000;   break;
        case 10000:  *nextp = 25000;   break;
        case 25000:  *nextp = 100000;  break;
        case 100000: *nextp = 500000;  break;
        case 500000: *nextp = 1000000; break;
        default:     *nextp = ~((__u64)0);
        }
        if (next < METRIC_TRIGGER_SEGS)
            return 1;
        break;
    case BPF_SOCK_OPS_STATE_CB: {
        int state = ops->args[1];
        switch (state) {
        case BPF_TCP_FIN_WAIT1:
        case BPF_TCP_CLOSE_WAIT:
            break;
        default:
            return 1;
        }
        break;
    }
    default:
        return 1;
    }

    if (!sk)
        return 1;
    tp = bpf_skc_to_tcp_sock(sk);
    if (ops->family == AF_INET) {
        __u32 ip4 = bpf_ntohl(ops->remote_ip4);
        if ((ip4 & 0xff000000) == 0x7f000000) return 1;
        if ((ip4 & 0xffff0000) == 0xa9fe0000) return 1;
    } else if (ops->family == AF_INET6) {
        if (ops->remote_ip6[0] == 0 && ops->remote_ip6[1] == 0 && ops->remote_ip6[2] == 0 && ops->remote_ip6[3] == bpf_htonl(1)) return 1;
        if ((ops->remote_ip6[0] & bpf_htonl(0xffc00000)) == bpf_htonl(0xfe800000)) return 1;
    }
    switch (ops->family) {
    case AF_INET:
        key->s6_addr32[2] = bpf_htonl(0xffff);
        key->s6_addr32[3] = ops->remote_ip4;
        break;
    case AF_INET6:
        key->s6_addr32[0] = ops->remote_ip6[0];
        key->s6_addr32[1] = ops->remote_ip6[1];
        key->s6_addr32[2] = ops->remote_ip6[2];
        key->s6_addr32[3] = ops->remote_ip6[3];
        break;
    default:
        return 1;
    }
    remote_host = get_remote_host(key, false);
    if (!remote_host)
        return 1;
    if (!tp)
        return 1;

    is_close = (ops->op == BPF_SOCK_OPS_STATE_CB);
    statep = bpf_sk_storage_get(&sk_storage_map, sk, 0, 0);
    if (!statep)
        return 1;
    s = statep->state & (NUM_TCP_CONG_ALGS - 1);
    if ((__u64)tp->segs_out + tp->segs_in < METRIC_MIN_SEGS)
        return 1;
    if (is_close)
        bpf_printk("closport port=%u rport=%u segs=%llu",
                   ops->local_port, bpf_ntohl(ops->remote_port),
                   (__u64)tp->segs_out + tp->segs_in);
    if (is_close &&
        (__u64)tp->segs_out + tp->segs_in >= METRIC_TRIGGER_SEGS)
        return 1;
    min_rtt = (__u64)tp->rtt_min.s[0].v;
    avg_rtt = (__u64)(tp->srtt_us >> 3);
    rate_interval_us = (__u64)tp->rate_interval_us;
    rate_delivered = (__u64)tp->rate_delivered;
    mss = (__u64)tp->mss_cache;
    rate_delivered = rate_interval_us ?
        (rate_delivered * mss * 1000000ULL) / rate_interval_us : 0;

    m = &remote_host->metrics[s];

    {
        __u64 rtt_term = 0, rate_term = 0;
        __u64 heal_rtt = 0, heal_rate = 0;
        metric = tcp_metric_calc(remote_host, min_rtt, avg_rtt, rate_delivered,
                                 &rtt_term, &rate_term, &heal_rtt, &heal_rate);
        bpf_printk("met rport=%u alg=%d segs=%llu val=%llu rtt=%llu rate=%llu smrtt=%llu bmrtt=%llu avgrtt=%llu",
                   bpf_ntohl(ops->remote_port),
                   s, (__u64)tp->segs_out + tp->segs_in, metric, rtt_term, rate_term,
                   min_rtt, remote_host->min_rtt, avg_rtt);
        if (heal_rtt)
            bpf_printk("heal_rtt smrtt=%llu newref=%llu",
                       (unsigned long long)min_rtt,
                       (unsigned long long)heal_rtt);
        if (heal_rate)
            bpf_printk("heal_rate rate=%llu newref=%llu",
                       (unsigned long long)rate_delivered,
                       (unsigned long long)heal_rate);
    }

    now = bpf_ktime_get_ns();

    if (statep && !is_close)
        statep->last_metric = metric;

    {
        __u64 best_alt = ~((__u64)0);
        __u8 best_alt_i = 0;

        if (statep && statep->best_alt_i < NUM_TCP_CONN_METRICS) {
            __u8 alt = (__u8)(statep->best_alt_i & (NUM_TCP_CONN_METRICS - 1));
            if (alt != s && remote_host->metrics[alt].metric_count > 0) {
                best_alt = remote_host->metrics[alt].metric_value;
                best_alt_i = alt;
            }
        }
        if (best_alt != ~((__u64)0) && best_alt < m->metric_value)
            greedy = false;

        if (statep && !is_close && statep->swap_count < SWAP_MAX &&
            statep->pending_swap == 0 &&
            now >= statep->settle_until &&
            best_alt != ~((__u64)0) &&
            statep->last_metric != 0 &&
            statep->last_metric * 100 >= best_alt * SWAP_MARGIN_PCT) {
            statep->bad_checkpoints++;
            if (statep->bad_checkpoints >= SWAP_BAD_BEFORE) {
                statep->pending_swap = (__u64)best_alt_i + 1;
                statep->bad_checkpoints = 0;
            }
        } else if (statep) {
            statep->bad_checkpoints = 0;
        }
    }

    {
        __u64 __div = m->metric_count + 1;
        if (__div > METRIC_AVG_CAP)
            __div = METRIC_AVG_CAP;
        if (metric > m->metric_value)
            m->metric_value += (metric - m->metric_value) / __div;
        else
            m->metric_value -= (m->metric_value - metric) / __div;
    }
    m->metric_count++;
    if (greedy)
        m->greedy_count++;
    return 1;
}


SEC("sockops")
int bpftune_conn_tuner_swap(struct bpf_sock_ops *ops)
{
	struct bpf_sock *sk = ops->sk;
	struct conn_state *statep;
	__u64 pending;

	if (ops->op != BPF_SOCK_OPS_RTT_CB)
		return 1;
	if (!sk)
		return 1;
	statep = bpf_sk_storage_get(&sk_storage_map, sk, 0, 0);
	if (!statep)
		return 1;
	pending = statep->pending_swap;
	if (!pending)
		return 1;
	{
		__u8 algo = (__u8)((pending - 1) & (NUM_TCP_CONG_ALGS - 1));
		int sret = set_cong(ops, algo);
		bpf_printk("swap-attempt algo=%u pending=%llu ret=%d", algo, pending, sret);
		if (!sret) {
			statep->swap_count++;
			statep->settle_until = bpf_ktime_get_ns() + T_SETTLE_NS;
                        statep->last_metric = 0;
			bpf_printk("swap-exec algo=%u pending=%llu", algo, pending);
		}
		statep->pending_swap = 0;
	}
	return 1;
}
