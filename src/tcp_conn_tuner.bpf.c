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

BPF_MAP_DEF(remote_host_map, BPF_MAP_TYPE_HASH, struct in6_addr, struct remote_host, 1024, 0);

BPF_MAP_DEF(sk_storage_map, BPF_MAP_TYPE_SK_STORAGE, int, __u64, 0, BPF_F_NO_PREALLOC);

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
        __u64 *statep;

        if (!sk)
                return 0;
        statep = bpf_sk_storage_get(&sk_storage_map, sk, 0,
                                    BPF_SK_STORAGE_GET_F_CREATE);
        if (statep)
                *statep = (__u64)i;
        return 0;
}

__u64 tcp_thin_lto_choices;

SEC("sockops")
int bpftune_conn_tuner(struct bpf_sock_ops *ops)
{
	int cb_flags = BPF_SOCK_OPS_STATE_CB_FLAG|BPF_SOCK_OPS_RETRANS_CB_FLAG;
	struct remote_host *remote_host;
	struct in6_addr raddr = {};
	struct in6_addr *key = &raddr;
	struct bpf_sock *sk = ops->sk;
	struct rtable *rtable = NULL;
	struct rt6_info *rt6 = NULL;
	struct tcp_sock *tp = NULL;
	unsigned int rt_flags = 0;
	__u64 *statep = NULL;
	bool initial = false;
	int state;

	switch (ops->op) {
	case BPF_SOCK_OPS_ACTIVE_ESTABLISHED_CB:
	case BPF_SOCK_OPS_PASSIVE_ESTABLISHED_CB:
		/* enable other needed events */
		bpf_sock_ops_cb_flags_set(ops, cb_flags);
		initial = true;
		break;
	case BPF_SOCK_OPS_RETRANS_CB:
		/* set individual cong algorithm to BBR if retransmit rate
		 * is > 1/(2^DROP_SHIFT) of packets out.
		 */
		if (ops->total_retrans > (ops->segs_out >> DROP_SHIFT)) {
			if (sk) {
				statep = bpf_sk_storage_get(&sk_storage_map, sk,
							    0, 0);
			}
			if (!statep || *statep != TCP_STATE_CONG_BBR) {
				/* turn on TCP thin linear timeouts for lossy connections */
				if (!statep && !tcp_thin_lto) {
					int one = 1;

					if (!bpf_setsockopt(ops, SOL_TCP, TCP_THIN_LINEAR_TIMEOUTS,
							    &one, sizeof(one)))
						tcp_thin_lto_choices++;
				}
				(void)set_cong(ops, TCP_STATE_CONG_BBR);
				/* no more need for retrans events... */
				bpf_sock_ops_cb_flags_set(ops, BPF_SOCK_OPS_STATE_CB_FLAG);
			}
		}
		return 1;
	case BPF_SOCK_OPS_STATE_CB:
		state = ops->args[1];
		switch (state) {
		case BPF_TCP_FIN_WAIT1:
		case BPF_TCP_CLOSE_WAIT:
			if (!sk)
				return 1;
			break;
		default:
			return 1;
		}
		break;
	default:
		return 1;
	}

	if (!sk)
		return 1;
	tp = bpf_skc_to_tcp_sock(sk);
	if (tp) {
		/* get remote host info from cached destination gateway */
		rtable = (struct rtable *)BPFTUNE_CORE_READ((struct sock *)tp,
							    sk_dst_cache);
	}
	if (rtable) {
		switch (ops->family) {
		case AF_INET:
            if (!BPFTUNE_CORE_READ(rtable, rt_uses_gateway))
                    return 1;
			key->s6_addr32[2] = bpf_htonl(0xffff);
			key->s6_addr32[3] = BPFTUNE_CORE_READ(rtable, rt_gw4);
			break;
		case AF_INET6:
			rt6 = (struct rt6_info *)rtable;
			rt_flags = BPFTUNE_CORE_READ(rt6, rt6i_flags);
			if (!(rt_flags & RTF_GATEWAY))
				return 1;
			key->s6_addr32[0] = BPFTUNE_CORE_READ(rt6, rt6i_gateway.in6_u.u6_addr32[0]);
			key->s6_addr32[1] = BPFTUNE_CORE_READ(rt6, rt6i_gateway.in6_u.u6_addr32[1]);
			key->s6_addr32[2] = BPFTUNE_CORE_READ(rt6, rt6i_gateway.in6_u.u6_addr32[2]);
			key->s6_addr32[3] = BPFTUNE_CORE_READ(rt6, rt6i_gateway.in6_u.u6_addr32[3]);
			break;
		default:
			return 1;
		}
	}
	remote_host = get_remote_host(key, initial);
	/* no RL unless seen a number of times... */
	if (!remote_host)
		return 1;

	switch (ops->op) {
	case BPF_SOCK_OPS_ACTIVE_ESTABLISHED_CB:
	case BPF_SOCK_OPS_PASSIVE_ESTABLISHED_CB: {
		__u64 metric_value = 0, metric_min = ~((__u64)0x0);
		__u8 i, ncands = 0, minindex = 0, s;
		__u8 cands[NUM_TCP_CONN_METRICS];

		/* find best (minimum) metric and use cong alg based on it. */
		for (i = 0; i < NUM_TCP_CONN_METRICS; i++) {
			cands[i] = 0;
			metric_value = remote_host->metrics[i].metric_value;
			if (metric_value > metric_min)
				continue;
			else if (metric_value < metric_min) {
				cands[0] = i;
				ncands = 1;
			} else if (metric_value == metric_min) {
				cands[ncands] = i;
				ncands++;
			}
			metric_min = metric_value;
		}
		/* if multiple min values, choose randomly. */
		if (ncands > 1 && ncands <= NUM_TCP_CONN_METRICS) {
			__u32 choice = bpf_get_prandom_u32() % ncands;

			/* verifier complains about variable stack offset */
			switch (choice) {
			case 0:  minindex = cands[0];  break;
			case 1:  minindex = cands[1];  break;
			case 2:  minindex = cands[2];  break;
			case 3:  minindex = cands[3];  break;
			case 4:  minindex = cands[4];  break;
			case 5:  minindex = cands[5];  break;
			case 6:  minindex = cands[6];  break;
			case 7:  minindex = cands[7];  break;
			case 8:  minindex = cands[8];  break;
			case 9:  minindex = cands[9];  break;
			case 10: minindex = cands[10]; break;
			case 11: minindex = cands[11]; break;
			case 12: minindex = cands[12]; break;
			case 13: minindex = cands[13]; break;
			case 14: minindex = cands[14]; break;
			case 15: minindex = cands[15]; break;
			default: return 1;
			}
		} else if (ncands == 1) {
			minindex = cands[0];
		} else {
			return 1;
		}
		minindex &= (NUM_TCP_CONN_METRICS - 1);
		/* choose random alg 5% of the time (1/20) */
		s = epsilon_greedy(minindex, NUM_TCP_CONN_METRICS, 20);
		s &= (NUM_TCP_CONG_ALGS - 1);

		if (set_cong(ops, s))

		        remote_host->metrics[s].metric_value = ~((__u64)0);

		return 1;
	}
	case BPF_SOCK_OPS_STATE_CB: {
		/* update metric/send metric event on connection close. */
		__u64 metric, metric_old, min_rtt, rate_interval_us, rate_delivered, mss;
		struct tcp_conn_metric *m;
		bool greedy = true;
		__u8 i, s;

		if (!sk)
			return 1;
		if (!remote_host)
			return 1;
		/* retrieve state indicating which cong alg was set */
		statep = bpf_sk_storage_get(&sk_storage_map, sk, 0, 0);
		if (!statep)
			return 1;
		s = *statep & (NUM_TCP_CONG_ALGS - 1);
		if (!tp)
			return 1;
		min_rtt = (__u64)tp->rtt_min.s[0].v;
		rate_interval_us = (__u64)tp->rate_interval_us;
                rate_delivered = (__u64)tp->rate_delivered;
                mss = (__u64)tp->mss_cache;
                /* Scale to bytes/second.  Raw bytes/us truncates to 0 or
                 * 1 for most real connections, so every algorithm
                 * measures identically and the throughput term in the
                 * metric is silently inert.
                 */
                rate_delivered = rate_interval_us ?
                        (rate_delivered * mss * 1000000ULL) / rate_interval_us : 0;

		m = &remote_host->metrics[s];
		if (!m->min_rtt || min_rtt < m->min_rtt)
                	m->min_rtt = min_rtt;
                if (!m->max_rate_delivered || rate_delivered > m->max_rate_delivered)
                	m->max_rate_delivered = rate_delivered;

		{

		        __u64 rtt_term = 0, rate_term = 0;

		        metric = tcp_metric_calc(remote_host, min_rtt,

		                                 m->max_rate_delivered,

		                                 &rtt_term, &rate_term);

		        		        bpf_printk("met alg=%d segs=%llu val=%llu rtt=%llu rate=%llu smrtt=%llu bmrtt=%llu", s, (__u64)tp->segs_out + tp->segs_in, metric, rtt_term, rate_term, min_rtt, remote_host->min_rtt);

		}
		for (i = 0; i < NUM_TCP_CONN_METRICS; i++) {
			if (s == i)
				continue;
			if (remote_host->metrics[i].metric_value < m->metric_value) {
				greedy = false;
				break;
			}
		}
		metric_old = m->metric_value;
		m->metric_value = rl_update(metric_old, metric, BPFTUNE_BITSHIFT);
		m->metric_count++;
		if (greedy)
			m->greedy_count++;
		return 1;
	}
	default:
		return 1;
	}
	return 1;
}
