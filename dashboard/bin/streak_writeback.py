#!/usr/bin/env python3
"""streak_writeback.py — sustained → kernel streak correction."""
import subprocess, sys, json, struct, ipaddress, os, time
from collections import defaultdict

RATE_HIST_BINS=32; NUM_TCP_CONG_ALGS=16; NUM_TCP_CONN_METRICS=16
OFF_METRIC_BAD_STREAK=42; OFF_METRIC_NULL_STREAK=43; SIZEOF_TCP_CONN_METRIC=48
OFF_RATE_HIST=112; SIZEOF_RATE_HIST=4*RATE_HIST_BINS+8
OFF_METRICS_ARRAY=OFF_RATE_HIST+SIZEOF_RATE_HIST
SIZEOF_REMOTE_HOST=OFF_METRICS_ARRAY+(NUM_TCP_CONN_METRICS*SIZEOF_TCP_CONN_METRIC)
WRITEBACK_WINDOW=8
_MASK_BITS_V4=16; _MASK_BITS_V6=32
WB_THROTTLE_FILE='/tmp/.bpftune_writeback_last'; WB_THROTTLE_SEC=300
_PREFIX_FIELDS=('min_rtt','max_rate_delivered','instances','selection_count','best_i','best_v','second_i','second_v','rate_best_i','rate_best_v','rate_second_i','rate_second_v','rtt_low_streak','rtt_low_min')
_METRIC_FIELDS_U64=('state_flags','greedy_count','metric_count','metric_value')
_METRIC_FIELDS_U16=('sockets_alive','sockets_good','sockets_proved','rate_ema','swap_score')

def _key_args_for(ip_str):
    try: ip=ipaddress.ip_address(ip_str)
    except (ValueError,TypeError): return None
    if isinstance(ip,ipaddress.IPv4Address):
        if _MASK_BITS_V4<32:
            mask=(0xFFFFFFFF<<(32-_MASK_BITS_V4))&0xFFFFFFFF; masked_int=int(ip)&mask
        else: masked_int=int(ip)
        masked_bytes=ipaddress.IPv4Address(masked_int).packed
        raw=b'\x00'*10+b'\xff\xff'+masked_bytes
    else:
        if _MASK_BITS_V6<128:
            mask=((1<<_MASK_BITS_V6)-1)<<(128-_MASK_BITS_V6); masked_int=int(ip)&mask
        else: masked_int=int(ip)
        masked_bytes=ipaddress.IPv6Address(masked_int).packed; raw=masked_bytes
    return [f'{b:02x}' for b in raw]

def _bytes_to_args(buf): return [f'{b:02x}' for b in buf]

def _value_json_to_bytes(v):
    buf=bytearray()
    for f in _PREFIX_FIELDS: buf.extend(struct.pack('<Q',int(v.get(f,0) or 0)))
    rate=v.get('rate',{}) or {}; bins=rate.get('bins',[]) or []
    for i in range(RATE_HIST_BINS): buf.extend(struct.pack('<I',int(bins[i]) if i<len(bins) else 0))
    buf.extend(struct.pack('<Q',int(rate.get('total',0) or 0)))
    metrics=v.get('metrics',[]) or []
    for i in range(NUM_TCP_CONN_METRICS):
        m=metrics[i] if i<len(metrics) else {}
        for f in _METRIC_FIELDS_U64: buf.extend(struct.pack('<Q',int(m.get(f,0) or 0)))
        for f in _METRIC_FIELDS_U16: buf.extend(struct.pack('<H',int(m.get(f,0) or 0)))
        buf.extend(struct.pack('<B',int(m.get('bad_streak',0) or 0)))
        buf.extend(struct.pack('<B',int(m.get('null_streak',0) or 0)))
        buf.extend(b'\x00\x00\x00\x00')
    return bytes(buf)

def _patch_streaks(buf,alg_idx,bad,null):
    base=OFF_METRICS_ARRAY+(alg_idx*SIZEOF_TCP_CONN_METRIC)
    buf[base+OFF_METRIC_BAD_STREAK]=bad&0xff; buf[base+OFF_METRIC_NULL_STREAK]=null&0xff

def _streaks_from_history(outcomes):
    bad=0; null=0
    for o in outcomes:
        if o=='win': bad=0; null=0
        elif o=='loss': bad+=1; null=0
        else: null+=1
    if bad>255: bad=255
    if null>255: null=255
    return bad,null

def _resolve_remote_host_map_id():
    try: out=subprocess.check_output(['bpftool','map','show'],text=True)
    except (subprocess.CalledProcessError,FileNotFoundError): return None
    for line in out.splitlines():
        if 'remote_host' in line and ':' in line:
            try: return int(line.split(':')[0].strip())
            except ValueError: continue
    return None

def _map_lookup_json(map_id,key_args):
    try:
        out=subprocess.check_output(['bpftool','map','lookup','id',str(map_id),'key','hex']+key_args,text=True,stderr=subprocess.STDOUT)
    except subprocess.CalledProcessError: return None
    try: return json.loads(out).get('value')
    except json.JSONDecodeError: return None

def _map_update_bytes(map_id,key_args,value_args):
    r=subprocess.run(['bpftool','map','update','id',str(map_id),'key','hex']+key_args+['value','hex']+value_args,capture_output=True,text=True)
    if r.returncode!=0: print(f'[writeback] WARN map update failed: {r.stderr.strip()}',file=sys.stderr)
    return r.returncode==0

def _detect_mask(map_id):
    global _MASK_BITS_V4,_MASK_BITS_V6
    try:
        out=subprocess.check_output(['bpftool','map','dump','id',str(map_id)],text=True,stderr=subprocess.STDOUT,timeout=10)
        data=json.loads(out)
    except Exception: return
    v4=[]; v6=[]
    for entry in data:
        ko=entry.get('key',{})
        if isinstance(ko,list): a8=[int(x) for x in ko]
        elif isinstance(ko,dict):
            in6=ko.get('in6_u',{}) or {}; a8=in6.get('u6_addr8',[]) or []
        else: continue
        if len(a8)!=16: continue
        if a8[10]==255 and a8[11]==255: v4.append(tuple(a8[12:16]))
        else: v6.append(tuple(a8))
    if v4:
        for bits in (8,16,24,32):
            idx=bits//8
            if all(all(o==0 for o in e[idx:]) for e in v4):
                if bits!=_MASK_BITS_V4:
                    _MASK_BITS_V4=bits; print(f'[writeback] /{bits} v4 mask ({len(v4)} entries)',file=sys.stderr)
                break
    if v6:
        for bits in (32,48,64,128):
            nbytes=bits//8
            if all(all(b==0 for b in e[nbytes:]) for e in v6):
                if bits!=_MASK_BITS_V6:
                    _MASK_BITS_V6=bits; print(f'[writeback] /{bits} v6 mask ({len(v6)} entries)',file=sys.stderr)
                break
        if _MASK_BITS_V6>32:
            print(f'[writeback] WARNING: v6 mask /{_MASK_BITS_V6} > /32 — BPF log only records 32 bits of dest6',file=sys.stderr)



def _masked_ip(ip_str):
    """Mask an IP to match the BPF map key (auto-detected prefix)."""
    try:
        ip = ipaddress.ip_address(ip_str)
    except (ValueError, TypeError):
        return ip_str
    if isinstance(ip, ipaddress.IPv4Address):
        if _MASK_BITS_V4 < 32:
            mask = (0xFFFFFFFF << (32 - _MASK_BITS_V4)) & 0xFFFFFFFF
            return str(ipaddress.IPv4Address(int(ip) & mask))
        return str(ip)
    else:
        if _MASK_BITS_V6 < 128:
            mask = ((1 << _MASK_BITS_V6) - 1) << (128 - _MASK_BITS_V6)
            return str(ipaddress.IPv6Address(int(ip) & mask))
        return str(ip)


def writeback_streaks(swaps):
    try:
        if os.path.exists(WB_THROTTLE_FILE):
            age=time.time()-os.path.getmtime(WB_THROTTLE_FILE)
            if age<WB_THROTTLE_SEC: return
    except OSError: pass
    map_id=_resolve_remote_host_map_id()
    if map_id is None: return
    _detect_mask(map_id)
    last_ts=0.0
    try:
        with open(WB_THROTTLE_FILE,'r') as f: last_ts=float(f.read().strip() or 0)
    except (OSError,ValueError): pass
    new_swaps=[s for s in swaps if s.get('outcome_sustained') and s.get('ts',0)>last_ts]
    if not new_swaps:
        try: os.utime(WB_THROTTLE_FILE,None)
        except OSError:
            try: open(WB_THROTTLE_FILE,'w').close()
            except OSError: pass
        return
    by_host=defaultdict(list); skipped_no_ip=0
    for s in new_swaps:
        rh=_masked_ip(s.get('remote_host'))
        if not rh: skipped_no_ip+=1; continue
        by_host[rh].append(s)
    if skipped_no_ip: print(f'[writeback] {skipped_no_ip} new swaps skipped (no remote_host)',file=sys.stderr)
    total_hosts=0; total_slots=0
    for host,host_new_swaps in by_host.items():
        key_args=_key_args_for(host)
        if not key_args: continue
        value_json=_map_lookup_json(map_id,key_args)
        if not value_json: continue
        buf=bytearray(_value_json_to_bytes(value_json))
        if len(buf)!=SIZEOF_REMOTE_HOST:
            print(f'[writeback] WARN: {host} buf {len(buf)}!={SIZEOF_REMOTE_HOST}; skip',file=sys.stderr); continue
        new_algs=set(s.get('to_alg') for s in host_new_swaps if s.get('to_alg') is not None)
        patches=0
        for alg_idx in new_algs:
            recent=sorted([s for s in swaps if _masked_ip(s.get('remote_host') or '')==host and s.get('to_alg')==alg_idx and s.get('outcome_sustained')],key=lambda x:x.get('ts',0))[-WRITEBACK_WINDOW:]
            if not recent: continue
            outcomes=[s.get('outcome_sustained') for s in recent]
            bad,null=_streaks_from_history(outcomes)
            _patch_streaks(buf,alg_idx,bad,null); patches+=1
        if patches==0: continue
        value_args=_bytes_to_args(bytes(buf))
        if _map_update_bytes(map_id,key_args,value_args):
            total_hosts+=1; total_slots+=patches
            print(f'[writeback] {host}: patched {patches} alg slots',file=sys.stderr)
    print(f'[writeback] done: {total_hosts} hosts, {total_slots} algorithm slots corrected (processed {len(new_swaps)} new swaps)',file=sys.stderr)
    max_ts=max(s.get('ts',0) for s in new_swaps)
    try:
        with open(WB_THROTTLE_FILE,'w') as f: f.write(str(max_ts))
    except OSError: pass

if __name__=='__main__':
    mid=_resolve_remote_host_map_id()
    if mid is None: print('FAIL: no map'); sys.exit(1)
    print(f'OK: map id={mid}')
    out=subprocess.run(['bpftool','map','dump','id',str(mid)],capture_output=True,text=True)
    try: data=json.loads(out.stdout)
    except: print('FAIL: JSON parse'); sys.exit(1)
    print(f'Map has {len(data)} entries')
    checked=0
    for entry in data:
        ko=entry.get('key',{})
        if isinstance(ko,list): a8=[int(x) for x in ko]
        elif isinstance(ko,dict): a8=(ko.get('in6_u',{}) or {}).get('u6_addr8',[]) or []
        else: continue
        if len(a8)==16 and a8[10]==255 and a8[11]==255 and any(a8[12:16]):
            ip='.'.join(str(b) for b in a8[12:16])
            v=_map_lookup_json(mid,_key_args_for(ip))
            if v:
                buf=_value_json_to_bytes(v); actual=len(buf)
                print(f'  {ip}: {actual} bytes [{"PASS" if actual==SIZEOF_REMOTE_HOST else "FAIL"}]')
                checked+=1
                if checked>=3: break
