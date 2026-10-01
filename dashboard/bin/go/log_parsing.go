package main

import ("os";"path/filepath";"sort";"strconv";"strings")

func (c *Collector) parseLogs() ([]interface{}, []interface{}, map[string]interface{}) {
	files, _ := filepath.Glob("/var/log/bpftune-met-*.log")
	sort.Slice(files, func(i, j int) bool { fi, _ := os.Stat(files[i]); fj, _ := os.Stat(files[j]); return fi.ModTime().After(fj.ModTime()) })
	var ns, np []interface{}
	for _, p := range files {
		i, _ := os.Stat(p); sz := i.Size()
		c.mu.Lock(); pv, ex := c.logOffsets[p]; c.mu.Unlock()
		if !ex { if sz > 2097152 { pv = sz - 2097152 } else { pv = 0 } }
		if sz < pv { pv = 0 }
		if pv >= sz { continue }
		f, _ := os.Open(p); f.Seek(pv, 0); b := make([]byte, sz-pv); n, _ := f.Read(b); f.Close()
		c.mu.Lock(); c.logOffsets[p] = sz; c.mu.Unlock()
		for _, ln := range strings.Split(string(b[:n]), "\n") {
			ln = strings.TrimSpace(ln); if ln == "" { continue }
			kv := parseKV(ln)
			if strings.Contains(ln, " swap ") {
				ns = append(ns, map[string]interface{}{"boot_ts": pf(kv["boot_ts"]), "from_alg": kv["from_alg"], "to_alg": kv["to_alg"], "dest": kv["dest"], "_bucket": kv["_bucket"], "d": pi(kv["d"]), "outcome": kv["outcome"], "outcome_srate": kv["outcome_srate"], "outcome_sustained": kv["outcome_sustained"], "mt_alg": kv["mt_alg"], "rb_alg": kv["rb_alg"]})
			} else if strings.Contains(ln, " proof ") {
				np = append(np, map[string]interface{}{"boot_ts": pf(kv["boot_ts"]), "alg": kv["alg"], "mbps": pf(kv["mbps"]), "dest": kv["dest"], "tier": kv["tier"]})
			}
		}
	}
	c.mu.Lock()
	for _, s := range ns { c.recentSwaps = append([]interface{}{s}, c.recentSwaps...) }
	if len(c.recentSwaps) > 50 { c.recentSwaps = c.recentSwaps[:50] }
	for _, p := range np { c.recentProofs = append([]interface{}{p}, c.recentProofs...) }
	if len(c.recentProofs) > 50 { c.recentProofs = c.recentProofs[:50] }
	as := c.recentSwaps; ap := c.recentProofs; c.mu.Unlock()
	cw, cl, cn, sw, sl, sn := 0, 0, 0, 0, 0, 0
	for _, s := range as {
		sm, _ := s.(map[string]interface{})
		o := toString(sm["outcome"])
		if o == "win" { cw++ } else if o == "loss" { cl++ } else { cn++ }
		os2 := toString(sm["outcome_sustained"])
		if os2 == "win" { sw++ } else if os2 == "loss" { sl++ } else { sn++ }
	}
	mC := cw + cl + cn; mS := sw + sl + sn
	so := map[string]interface{}{
		"composite": map[string]interface{}{"measurable": mC, "win": cw, "win_pct": pct(cw, mC), "loss": cl, "loss_pct": pct(cl, mC), "null": cn, "null_pct": pct(cn, mC)},
		"sustained": map[string]interface{}{"measurable": mS, "win": sw, "win_pct": pct(sw, mS), "loss": sl, "loss_pct": pct(sl, mS), "null": sn, "null_pct": pct(sn, mS)},
		"swaps_list": as,
	}
	var ts, tp []interface{}
	for i, s := range as { if i >= 20 { break }; ts = append(ts, s) }
	for i, p := range ap { if i >= 18 { break }; tp = append(tp, p) }
	return ts, tp, so
}
func parseKV(s string) map[string]string {
	m := make(map[string]string)
	for _, f := range strings.Fields(s) { if i := strings.Index(f, "="); i > 0 { m[f[:i]] = f[i+1:] } }
	return m
}
func pf(s string) float64 { v, _ := strconv.ParseFloat(s, 64); return v }
func pi(s string) int { v, _ := strconv.Atoi(s); return v }
func pct(n, d int) float64 { if d == 0 { return 0 }; return float64(n) * 100.0 / float64(d) }
