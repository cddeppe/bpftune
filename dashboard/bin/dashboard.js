(function () {
  var PALETTE = [
    "#4e79a7", "#f28e2c", "#e15759", "#76b7b2",
    "#59a14f", "#edc949", "#af7aa1", "#ff9da7",
    "#9c755f", "#bab0ab", "#1b9e77", "#d95f02",
    "#7570b3", "#e7298a", "#66a61e", "#e6ab02"
  ];

  var gen = document.getElementById("gen");
  var footgen = document.getElementById("footgen");

  function status(msg, isErr) {
    if (gen) {
      gen.textContent = msg;
      gen.style.color = isErr ? "#e5484d" : "";
      if (!isErr) {
        gen.classList.add("flash");
        setTimeout(function () { gen.classList.remove("flash"); }, 300);
      }
    }
  }
  function err(msg, e) { status(msg, true); if (e) console.error(msg, e); }

  function loadScript(url) {
    return new Promise(function (resolve, reject) {
      var s = document.createElement("script");
      s.src = url;
      s.onload = resolve;
      s.onerror = function () { reject(new Error("failed to load " + url)); };
      document.head.appendChild(s);
    });
  }

  function applyChartDefaults() {
    var dark = window.matchMedia &&
               window.matchMedia("(prefers-color-scheme: dark)").matches;
    var grid = dark ? "rgba(255,255,255,.06)" : "rgba(20,30,50,.06)";
    var tick = dark ? "#8b929b" : "#6b7280";
    Chart.defaults.font.family =
      "system-ui, -apple-system, 'Segoe UI', Roboto, sans-serif";
    Chart.defaults.font.size = 11;
    Chart.defaults.color = tick;
    Chart.defaults.borderColor = grid;
    Chart.defaults.elements.line.borderWidth = 1.5;
    Chart.defaults.elements.point.radius = 0;
    Chart.defaults.elements.point.hoverRadius = 3;
    Chart.defaults.animation = false;
    Chart.defaults.plugins.legend.labels.boxWidth = 10;
    Chart.defaults.plugins.legend.labels.boxHeight = 10;
    Chart.defaults.plugins.legend.labels.padding = 8;
    Chart.defaults.plugins.tooltip.backgroundColor = dark ? "#1c2028" : "#fff";
    Chart.defaults.plugins.tooltip.borderColor = dark ? "#2a2f39" : "#e5e7eb";
    Chart.defaults.plugins.tooltip.borderWidth = 1;
    Chart.defaults.plugins.tooltip.titleColor = dark ? "#e6e8eb" : "#131720";
    Chart.defaults.plugins.tooltip.bodyColor  = dark ? "#e6e8eb" : "#131720";
    Chart.defaults.plugins.tooltip.padding = 8;
    Chart.defaults.plugins.tooltip.cornerRadius = 6;
  }

  function $(id) { return document.getElementById(id); }
  function setHTML(id, s) { var e = $(id); if (e) e.innerHTML = s; }
  /* 0.4.79: monotonic-age label for recent swaps / proofs. */
  var SERVER_NOW_MONO = 0;
  function ageLabel(boot_ts) {
    if (!boot_ts || !SERVER_NOW_MONO) return '';
    var age = SERVER_NOW_MONO - boot_ts;
    if (age < 0) return '';
    if (age < 60)    return Math.round(age) + 's';
    if (age < 3600)  return Math.round(age/60) + 'm';
    if (age < 86400) return Math.round(age/3600) + 'h';
    return Math.round(age/86400) + 'd';
  }

  function esc(s) {
    return String(s == null ? "" : s).replace(/[&<>"']/g, function (c) {
      return ({ "&": "&amp;", "<": "&lt;", ">": "&gt;",
                '"': "&quot;", "'": "&#39;" })[c];
    });
  }
  function fmtN(v) {
    return (v == null) ? "-" : Math.round(v).toLocaleString();
  }
  function fmtMbps(v) {
    return (v == null) ? "-" : (v / 125000).toFixed(1);
  }
  function fmtRe(v) {
    return (v == null) ? "-" : (v * 0.8).toFixed(1);
  }
  function scaleRe(v) {
    return v == null ? null : v * 0.8;
  }
  function fmtRTT(v) {
    return (v == null) ? "-" : (v / 1000).toFixed(1) + " ms";
  }
  function fmtBytes(b) {
    if (b == null) return "-";
    if (b >= 1e9) return (b / 1e9).toFixed(2) + " GB";
    if (b >= 1e6) return (b / 1e6).toFixed(1) + " MB";
    return Math.round(b) + " B";
  }
  function fmtUptime(s) {
    if (s == null) return "-";
    var d = Math.floor(s / 86400);
    var h = Math.floor((s % 86400) / 3600);
    var m = Math.floor((s % 3600) / 60);
    return (d ? d + "d " : "") + h + "h " + m + "m";
  }
  function relTime(epochSec) {
    if (!epochSec) return "";
    var dt = Math.max(0, Math.floor(Date.now() / 1000) - epochSec);
    if (dt < 60) return dt + "s ago";
    if (dt < 3600) return Math.floor(dt / 60) + "m ago";
    if (dt < 86400) return Math.floor(dt / 3600) + "h ago";
    return Math.floor(dt / 86400) + "d ago";
  }

  function renderBuild(b) {
    var rows = [
      ["version",   b.version, "hi"],
      ["dashboard", b.dash_version || "?", "hi"],
      ["service",   b.service, b.service === "active" ? "hi" : ""],
    ];
    if (b.uptime_min != null) {
      var h = Math.floor(b.uptime_min / 60);
      var m = b.uptime_min % 60;
      rows.push(["uptime", h + "h " + m + "m"]);
    }
    if (b.started_utc) rows.push(["started", b.started_utc + " UTC", "dim"]);
    if (b.log_path)   rows.push(["log", b.log_path, "dim"]);
    setHTML("lv-build", rows.map(function (r) {
      return '<div class="row"><span class="k">' + esc(r[0]) + '</span>' +
             '<span class="v ' + (r[2] || "") + '">' + esc(r[1]) + '</span></div>';
    }).join(""));
  }

  function renderSystem(s) {
    var rows = [];
    if (s.kernel)     rows.push(["kernel", s.kernel, "hi"]);
    if (s.default_cc) rows.push(["default cc", s.default_cc, "hi"]);
    if (s.cpu_count != null) rows.push(["cpu", s.cpu_count + " cores"]);
    if (s.load_1 != null) {
      rows.push(["load",
        s.load_1.toFixed(2) + " / " + s.load_5.toFixed(2) +
        " / " + s.load_15.toFixed(2)]);
    }
    if (s.procs_total != null) {
      rows.push(["processes",
        (s.procs_running == null ? "?" : s.procs_running) +
        " running / " + s.procs_total + " total"]);
    }
    if (s.host_uptime_s != null) {
      rows.push(["host uptime", fmtUptime(s.host_uptime_s)]);
    }
    if (s.mem_total_bytes != null && s.mem_total_bytes > 0) {
      var used = (s.mem_used_bytes != null)
                 ? s.mem_used_bytes
                 : (s.mem_total_bytes - (s.mem_avail_bytes || 0));
      var pct = (s.mem_used_pct != null)
                ? s.mem_used_pct.toFixed(0) + "%"
                : "";
      rows.push(["memory",
        fmtBytes(used) + " / " + fmtBytes(s.mem_total_bytes) +
        (pct ? "  " + pct : "")]);
    }
    setHTML("lv-system", rows.map(function (r) {
      return '<div class="row"><span class="k">' + esc(r[0]) + '</span>' +
             '<span class="v ' + (r[2] || "") + '">' + esc(r[1]) + '</span></div>';
    }).join(""));
  }

  function renderTunables(groups) {
    var cnt = $("lv-tun-cnt");
    if (!groups || !groups.length) {
      if (cnt) cnt.textContent = "";
      setHTML("lv-tunables",
              '<div class="placeholder">(none seen in journal this boot)</div>');
      return;
    }
    var total = 0;
    groups.forEach(function (g) { total += g.items.length; });
    if (cnt) cnt.textContent = total + " keys · " + groups.length + " groups";
    setHTML("lv-tunables", groups.map(function (g) {
      var rows = g.items.map(function (it) {
        return '<div class="grow"><span class="k">' + esc(it.key) +
               '</span><span class="v">' + esc(it.value) + '</span></div>';
      }).join("");
      return '<div class="tun-group"><div class="gname">' +
             esc(g.group) + '</div>' + rows + '</div>';
    }).join(""));
  }

  function renderBuckets(rows) {
    state.lastBucketRows = rows || [];
    if (!rows.length) {
      setHTML("lv-buckets", '<div class="placeholder">(no buckets)</div>');
      return;
    }
    var cov = {};
    var f = state.fleet || {};
    var fb = f.buckets || [];
    var fc = f.coverage_24h || [];
    for (var i = 0; i < fb.length; i++) cov[fb[i]] = fc[i];

    var html = '<table class="tbl"><thead><tr>' +
      '<th>dest</th><th>instances</th><th>min rtt</th>' +
      '<th>ref rate</th><th>best alg</th><th>algs</th>' +
      '<th style="width:24%">coverage · 24h</th>' +
      '</tr></thead><tbody>';
    rows.forEach(function (r) {
      var v = cov[r.dest];
      var cell;
      if (v == null) {
        cell = '<td class="mono dim">&ndash;</td>';
      } else {
        var pct = Math.max(0, Math.min(100, v));
        cell = '<td class="mono"><span class="covbar"><i style="width:' +
               pct.toFixed(0) + '%"></i></span>' + pct.toFixed(0) + '%</td>';
      }
      html += '<tr>' +
        '<td class="mono name">' + esc(shortAddr(r.dest)) + '</td>' +
        '<td class="mono">' + fmtN(r.inst) + '</td>' +
        '<td class="mono dim">' + (r.rtt_us / 1000).toFixed(1) + ' ms</td>' +
        '<td class="mono">' + r.ref_mbps.toFixed(1) + '</td>' +
        '<td>' + esc(r.best_alg) + '</td>' +
        '<td class="mono dim">' + r.n_alg + '</td>' +
        cell +
        '</tr>';
    });
    setHTML("lv-buckets", html + '</tbody></table>');
  }

  function renderRecentSwapsForBucket() {
    var by = state.recentSwapsByBucket;
    if (!by) {
      // fallback: pristine behaviour, unfiltered recent swaps
      renderRecentSwaps((state.lastLiveSwaps) || []);
      return;
    }
    var bs = $("bucket");
    var addr = (bs && bs.value) ? bs.value : null;
    // "All Buckets" selected — show all swaps across all buckets
    if (addr === 'all') {
      renderRecentSwaps(state.lastLiveSwaps || []);
      return;
    }
    if (!addr && state.meta && state.meta.default_bucket) {
      addr = state.meta.default_bucket;
    }
    var rows = (addr && by[addr]) ? by[addr] : [];
    renderRecentSwaps(rows);
  }

  function renderMetricForBucket() {
    var bs = $("bucket");
    var addr = (bs && bs.value) ? bs.value : null;
    if (!addr && state.meta && state.meta.default_bucket) {
      addr = state.meta.default_bucket;
    }
    var byB = state.metricByBucket || {};
    var keys = Object.keys(byB);
    var rows = (addr && byB[addr]) ? byB[addr]
                                   : (keys.length ? byB[keys[0]] : []);
    renderMetric(rows);
  }

  function renderMetric(rows) {
    if (!rows.length) {
      setHTML("lv-metric", '<div class="placeholder">(no metrics yet)</div>');
      return;
    }
    function colorSwapScore(v) {
      if (v == null) return "cell-dim";
      if (v > 256) return "cell-good";
      if (v < 256) return "cell-bad";
      return "cell-dim";
    }
    function colorPenalty(v) {
      if (v == null) return "cell-dim";
      if (v >= 0.99) return "cell-dim";
      if (v >= 0.8)  return "";
      if (v >= 0.5)  return "cell-bad";
      return "cell-bad";
    }
    function colorStreak(v) {
      if (v == null) return "cell-dim";
      if (v === 0) return "cell-good";
      return "cell-bad";
    }
    // Top row is the picker's choice: sorted by score in the CLI, so
    // the first non-inactive row wins.
    var picked = null;
    for (var i = 0; i < rows.length; i++) {
      if (rows[i].active) { picked = i; break; }
    }
    var html = '<table class="tbl"><thead><tr>' +
      '<th>alg</th><th>Rate EMA<br>'
        + '<span style="font-weight:400;text-transform:none;letter-spacing:0">Mb/s</span></th>'
        + '<th>Swap Score</th>' +
      '<th>penalty</th><th>score</th><th>metric</th>' +
      '<th>bad</th><th>null</th>' +
      '</tr></thead><tbody>';
    rows.forEach(function (r, i) {
      var cls = [];
      if (!r.active) cls.push("inactive");
      if (i === picked) cls.push("pick");
      var trClass = cls.length ? ' class="' + cls.join(" ") + '"' : "";
      var pen = (r.penalty == null) ? "-" : r.penalty.toFixed(3);
      html += '<tr' + trClass + '>' +
        '<td class="name">' + esc(r.alg) + '</td>' +
        '<td class="mono">' +
          (r.rate_ema == null ? "-"
           : fmtRe(r.rate_ema)
             + '<span class="dim" style="font-weight:400"> ('
             + r.rate_ema + ')</span>') +
        '</td>' +
        '<td class="mono ' + colorSwapScore(r.swap_score) + '">' +
          (r.swap_score == null ? "-" : r.swap_score) + '</td>' +
        '<td class="mono ' + colorPenalty(r.penalty) + '">' + pen + '</td>' +
        '<td class="mono cell-good">' +
          (r.score == null ? "-" : r.score.toFixed(1)) + '</td>' +
        '<td class="mono dim">' + r.metric.toFixed(1) + '</td>' +
        '<td class="mono ' + colorStreak(r.bad_streak) + '">' +
          (r.bad_streak == null ? "-" : r.bad_streak) + '</td>' +
        '<td class="mono ' + colorStreak(r.null_streak) + '">' +
          (r.null_streak == null ? "-" : r.null_streak) + '</td>' +
        '</tr>';
    });
    setHTML("lv-metric", html + '</tbody></table>' +
      '<div class="note" style="margin-top:8px">' +
      'Rate EMA is <b>Mb/s</b> in this table, matching the chart and NOW. ' +
      'The raw 100&nbsp;KB/s value the picker uses is shown in parenthesis.' +
      '</div>');
  }

  function renderProof(rows) {
    if (!rows.length) {
      setHTML("lv-proof", '<div class="placeholder">(none in tail)</div>');
      return;
    }
    var peak = 1;
    rows.forEach(function (r) {
      [r.proven_max, r.sampled_avg, r.sampled_max].forEach(function (v) {
        if (v != null && v > peak) peak = v;
      });
    });
    function bar(v, cls) {
      // 0.4.78.2: covbar + fixed-width number in a single flex row
      // so the bar's left edge aligns across all rows.
      if (v == null) {
        return '<span class="proof-cell">' +
               '<span class="mono dim">-</span></span>';
      }
      var w = Math.max(2, Math.round(100 * v / peak));
      return '<span class="proof-cell">' +
             '<span class="mono">' + v.toFixed(1) + '</span>' +
             '<span class="covbar ' + cls + '">' +
             '<i style="width:' + w + '%"></i></span>' +
             '</span>';
    }
    var html = '<table class="tbl proof-tbl"><thead><tr>' +
      '<th>alg</th>' +
      '<th>good</th><th>proved</th>' +
      '<th>proven max</th>' +
      '<th>sustained avg</th>' +
      '<th>sustained max</th>' +
      '<th>n</th>' +
      '</tr></thead><tbody>';
    rows.forEach(function (r) {
      html += '<tr>' +
        '<td class="name">' + esc(r.alg) + '</td>' +
        '<td class="mono dim">' + r.good + '</td>' +
        '<td class="mono dim">' + r.proved + '</td>' +
        '<td>' + bar(r.proven_max,  'v-proven' ) + '</td>' +
        '<td>' + bar(r.sampled_avg, 'v-avg'    ) + '</td>' +
        '<td>' + bar(r.sampled_max, 'v-sampled') + '</td>' +
        '<td class="mono dim">' +
          (r.samples == null ? "-" : r.samples) + '</td>' +
        '</tr>';
    });
    setHTML("lv-proof", html + '</tbody></table>');
  }

  function renderRate(rows) {
    if (!rows.length) {
      setHTML("lv-rate", '<div class="placeholder">(no midsamp lines)</div>');
      return;
    }
    var html = '<table class="tbl"><thead><tr>' +
      '<th>thr</th><th>n</th><th>mean</th><th>min</th><th>max</th>' +
      '</tr></thead><tbody>';
    rows.forEach(function (r) {
      html += '<tr>' +
        '<td class="mono">' + fmtN(r.thr) + '</td>' +
        '<td class="mono dim">' + r.n + '</td>' +
        '<td class="mono">'   + r.mean.toFixed(1) + '</td>' +
        '<td class="mono dim">' + r.min.toFixed(1) + '</td>' +
        '<td class="mono">'   + r.max.toFixed(1) + '</td>' +
        '</tr>';
    });
    setHTML("lv-rate", html + '</tbody></table>');
  }

  function bigTriple(so) {
    return '<div class="bigstats">' +
      '<div class="big win"><div class="k">win</div>' +
        '<div class="v">' + so.win + '</div>' +
        '<div class="p">' + so.win_pct.toFixed(0) + '%</div></div>' +
      '<div class="big null"><div class="k">null</div>' +
        '<div class="v">' + so.null + '</div>' +
        '<div class="p">' + so.null_pct.toFixed(0) + '%</div></div>' +
      '<div class="big loss"><div class="k">loss</div>' +
        '<div class="v">' + so.loss + '</div>' +
        '<div class="p">' + so.loss_pct.toFixed(0) + '%</div></div>' +
      '</div>';
  }

  function renderSwapOutcomes(payload, churn) {
    var so = (payload && payload.sustained) ? payload.sustained
              : (payload || {win:0,win_pct:0,null:0,null_pct:0,
                             loss:0,loss_pct:0,measurable:0,unmeasurable:0,
                             rescued:0, full_loss:0, open:0});
    var ch = churn || {cookies:0, one:0, mid:0, many:0, max:0};
    var rescued = so.rescued || 0;
    var fullLoss = so.full_loss || 0;
    var openLoss = so.open || 0;
    var totalLoss = so.loss || 0;
    function pct(n, d) { return d > 0 ? Math.round(n / d * 100) : 0; }
    function cell(cls, val, p, k) {
      return '<div class="lr-cell ' + cls + '">' +
               '<div class="lr-v">' + val + '</div>' +
               '<div class="lr-pct">' + p + '%</div>' +
               '<div class="lr-k">' + k + '</div>' +
             '</div>';
    }
    function frow(k, v, dim) {
      return '<div class="row' + (dim ? ' dim' : '') + '"><span class="k">' + k + '</span>' +
             '<span class="v' + (dim ? ' dim' : '') + '">' + v + '</span></div>';
    }
    var hasLossData = (rescued || fullLoss || openLoss) > 0;
    var html = bigTriple(so);
    if (hasLossData) {
      html +=
        '<div class="loss-recovery">' +
          '<div class="lr-header">loss recovery \u2014 what happened to the ' + totalLoss + ' losses</div>' +
          '<div class="lr-cells">' +
            cell('rescued', rescued, pct(rescued, totalLoss), 'rescued') +
            cell('full',   fullLoss, pct(fullLoss, totalLoss), 'full loss') +
            cell('open',   openLoss, pct(openLoss, totalLoss), 'open') +
          '</div>' +
          '<div class="lr-cap">a later win on the same cookie closes a loss</div>' +
        '</div>';
    } else {
      html +=
        '<div class="loss-recovery placeholder">' +
          '<div class="lr-header">loss recovery \u2014 awaiting server-side data</div>' +
          '<div class="lr-cells">' +
            cell('rescued ph', '\u2014', 0, 'rescued') +
            cell('full   ph', '\u2014', 0, 'full loss') +
            cell('open   ph', '\u2014', 0, 'open') +
          '</div>' +
          '<div class="lr-cap">apply bpftune-cli.py patch to populate</div>' +
        '</div>';
    }
    html +=
      '<div class="lr-footer">' +
        frow('measurable', so.measurable || 0) +
        frow('cookies swapped', ch.cookies || 0) +
        frow('unmeasurable', so.unmeasurable || 0, true) +
        frow('one-off', ch.one || 0) +
        '<div class="row empty"></div>' +
        frow('2-4x', ch.mid || 0) +
        '<div class="row empty"></div>' +
        frow('5x+', ch.many || 0) +
        '<div class="row empty"></div>' +
        frow('max per cookie', ch.max || 0) +
      '</div>';
    setHTML("lv-swapout", html);
  }


  function renderDivergence(rows) {
    if (!rows.length) {
      setHTML("lv-div", '<div class="placeholder">(no swaps)</div>');
      return;
    }
    var html = '<table class="tbl"><thead><tr>' +
      '<th>category</th><th style="width:32%">composite</th>' +
      '<th style="width:32%">sustained</th>' +
      '<th>n</th>' +
      '</tr></thead><tbody>';
    rows.forEach(function (r) {
      function bar(w, n, l) {
        var out = '<div class="cellbar">';
        if ((w + n + l) > 0) {
          if (w > 0) out += '<span class="w" style="width:' + w + '%">' +
            (w >= 8 ? w.toFixed(0) + '%' : '') + '</span>';
          if (n > 0) out += '<span class="n" style="width:' + n + '%">' +
            (n >= 8 ? n.toFixed(0) + '%' : '') + '</span>';
          if (l > 0) out += '<span class="l" style="width:' + l + '%">' +
            (l >= 8 ? l.toFixed(0) + '%' : '') + '</span>';
        } else {
          out += '<span class="n" style="width:100%">no data</span>';
        }
        out += '</div>';
        return out;
      }
      html += '<tr>' +
        '<td class="name">' + esc(r.category) + '</td>' +
        '<td>' + bar(r.win_pct, r.null_pct, r.loss_pct) + '</td>' +
        '<td>' + bar(r.win_pct_sustained, r.null_pct_sustained,
                     r.loss_pct_sustained) + '</td>' +
        '<td class="mono dim">' + r.measured + ' / ' +
          r.measured_sustained + '</td>' +
        '</tr>';
    });
    setHTML("lv-div", html + '</tbody></table>');
  }

  function renderRecentProofs(rows) {
    if (!rows.length) {
      setHTML("lv-proofs", '<div class="placeholder">(none)</div>');
      return;
    }
    var html = '<div class="list">';
    rows.slice().reverse().forEach(function (r) {
      html += '<div class="item">' +
        '<span class="flow">' + esc(r.alg) + '</span>' +
        '<span class="meta">' + esc(shortAddr(r.dest)) +
          ' &middot; ' + r.mbps.toFixed(1) + ' Mb/s' +
          (r.boot_ts ? ' &middot; ' + ageLabel(r.boot_ts) : '') +
          '</span>' +
        '<span class="sp ' + r.tier + '">' + r.tier + '</span>' +
        '</div>';
    });
    setHTML("lv-proofs", html + '</div>');
  }

  function shortAddr(a) {
    a = a || "";
    if (window.__labels && window.__labels[a]) return window.__labels[a];
    if (a.indexOf("v6:") === 0) {
      var hex = a.substring(3);
      if (hex.length >= 8) {
        var ip6 = hex.substring(0,4) + ":" + hex.substring(4,8) + "::";
        if (window.__labels && window.__labels[ip6]) return window.__labels[ip6];
      }
      return a;
    }
    var p = a.split(".");
    if (p.length === 4) return p[0] + "." + p[1] + ".0.0";
    return a;
  }

  function renderRecentSwaps(rows) {
    if (!rows.length) {
      setHTML("lv-swaps", '<div class="placeholder">(none in tail)</div>');
      return;
    }
    var html = '<div class="list">';
    /* 0.4.79: rows arrive newest-first from the CLI.  Do NOT
     * reverse here -- that was flipping to oldest-first, so the
     * top of the panel showed hours-old swaps and looked
     * current.  Fixed 2026-09-26. */
    rows.forEach(function (r) {
      /* 0.4.79: only outcome_sustained is final.  The composite
       * outcome is provisional -- it may read null while the
       * sustained window is still open.  Show "pending" until the
       * collector has classified the swap on the sustained ruler
       * (T+60..T+300 after the swap). */
      var o = r.outcome_sustained || "";
      var pill = o
        ? '<span class="sp ' + o + '">' + o + '</span>'
        : (function() {
            var age = (r.boot_ts && SERVER_NOW_MONO) ? (SERVER_NOW_MONO - r.boot_ts) : 0;
            if (age < 60) return '<span class="sp wait">wait</span>';
            return '<span class="sp void">void</span>';
          })();
      html += '<div class="item">' +
        '<span class="flow">' + esc(r.from_alg) +
          '<span class="arrow">&rarr;</span>' + esc(r.to_alg) + '</span>' +
        '<span class="meta">' + esc(shortAddr(r.dest)) +
          ' &middot; d' + r.d +
          (r.boot_ts ? ' &middot; ' + ageLabel(r.boot_ts) : '') +
          '</span>' +
        pill +
        '</div>';
    });
    setHTML("lv-swaps", html + '</div>');
  }

  function renderLogWindow(doc) {
    var lw = doc.log_window || {};
    if (!lw.span_min) return;
    var text = 'rolling ' + lw.span_min + ' min \u00b7 ' + lw.swap_count + ' swaps \u00b7 newest ' + lw.age_min + 'm ago';
    ['time-window-outcomes', 'time-window-recent-swaps'].forEach(function(id) {
      var el = document.getElementById(id);
      if (el) el.textContent = text;
    });
  }

  function _filterByBucket(doc, bucketLabel) {
    if (bucketLabel === 'all' || !bucketLabel) return doc;
    var f = JSON.parse(JSON.stringify(doc));
    // Filter swap outcomes from swaps_list
    if (f.swap_outcomes && f.swap_outcomes.swaps_list) {
      var swaps = f.swap_outcomes.swaps_list.filter(function(s) {
        return s.dest === bucketLabel;
      });
      var c = {win: 0, null: 0, loss: 0, skip: 0};
      swaps.forEach(function(s) {
        var o = s.outcome_sustained;
        if (o === null || o === undefined) c.skip++;
        else c[o] = (c[o] || 0) + 1;
      });
      var total = c.win + c.null + c.loss;
      // Compute loss recovery from filtered swaps
      var losses = swaps.filter(function(s) { return s.outcome_sustained === 'loss'; });
      losses.sort(function(a, b) { return (a.ts || 0) - (b.ts || 0); });
      var lastTs = swaps.length ? Math.max.apply(null, swaps.map(function(s) { return s.ts || 0; })) : 0;
      var rescued = 0, fullLoss = 0, openLoss = 0;
      var WINDOW = 3600;
      losses.forEach(function(loss) {
        var lossTs = loss.ts || 0;
        var found = false;
        for (var i = 0; i < swaps.length; i++) {
          var s = swaps[i];
          if (s === loss) continue;
          var sTs = s.ts || 0;
          if (sTs <= lossTs) continue;
          if (sTs - lossTs > WINDOW) break;
          if (s.cookie === loss.cookie && s.outcome_sustained === 'win') { found = true; break; }
        }
        if (found) rescued++;
        else if ((lastTs - lossTs) > WINDOW) fullLoss++;
        else openLoss++;
      });
      var totalLoss = c.loss;
      f.swap_outcomes.sustained = {
        win: c.win, null: c.null, loss: c.loss,
        measurable: total, unmeasurable: c.skip,
        win_pct: total ? Math.round(c.win/total*100) : 0,
        null_pct: total ? Math.round(c.null/total*100) : 0,
        loss_pct: total ? Math.round(c.loss/total*100) : 0,
        rescued: rescued, full_loss: fullLoss, open: openLoss,
        rescued_pct: totalLoss ? Math.round(rescued/totalLoss*100) : 0,
        full_loss_pct: totalLoss ? Math.round(fullLoss/totalLoss*100) : 0,
        open_pct: totalLoss ? Math.round(openLoss/totalLoss*100) : 0,
      };
    }
    // Filter recent proofs
    if (f.recent_proofs) {
      f.recent_proofs = f.recent_proofs.filter(function(p) {
        return p.dest === bucketLabel;
      });
    }
    // Re-aggregate proof leaderboard from proofs_raw
    if (f.proofs_raw) {
      var fp = f.proofs_raw.filter(function(p) { return p.dest === bucketLabel; });
      var byAlg = {};
      fp.forEach(function(p) {
        if (!byAlg[p.alg]) byAlg[p.alg] = {good:0, proved:0, pmax:0, sum:0, n:0, smax:0};
        if (p.tier === 'good') byAlg[p.alg].good++;
        if (p.tier === 'proved') byAlg[p.alg].proved++;
        if (p.tier === 'proved' && p.rate > byAlg[p.alg].pmax) byAlg[p.alg].pmax = p.rate;
        byAlg[p.alg].sum += p.rate;
        byAlg[p.alg].n++;
        if (p.rate > byAlg[p.alg].smax) byAlg[p.alg].smax = p.rate;
      });
      f.proof = Object.keys(byAlg).map(function(alg) {
        var a = byAlg[alg];
        return {alg:alg, good:a.good, proved:a.proved,
          proven_max: a.pmax || null,
          sampled_avg: a.n ? Math.round(a.sum/a.n*10)/10 : null,
          sampled_max: a.smax || null,
          samples: a.n || null};
      }).sort(function(a,b) { return (b.proven_max||0) - (a.proven_max||0); });
    }
    // Re-aggregate rate progression from rate_raw
    if (f.rate_raw) {
      var fr = f.rate_raw.filter(function(r) { return r.dest === bucketLabel; });
      var byThr = {};
      fr.forEach(function(r) {
        if (!byThr[r.thr]) byThr[r.thr] = [];
        byThr[r.thr].push(r.srate);
      });
      f.rate = Object.keys(byThr).map(function(thr) {
        var vs = byThr[thr];
        var BPS = 125000;
        return {thr: parseInt(thr), n: vs.length,
          mean: Math.round(vs.reduce(function(a,b){return a+b},0)/vs.length/BPS*10)/10,
          min: Math.round(Math.min.apply(null,vs)/BPS*10)/10,
          max: Math.round(Math.max.apply(null,vs)/BPS*10)/10};
      }).sort(function(a,b) { return a.thr - b.thr; });
    }
    return f;
  }


  function _reFilterPanels() {
    var doc = window.__current_doc;
    if (!doc) return;
    var bid = $('bucket') ? $('bucket').value : 'all';
    var blabel = 'all';
    if (bid !== 'all') {
      var bs = $('bucket');
      blabel = (window.__labels && window.__labels[bid]) ||
        (bs && bs.selectedIndex >= 0 ? bs.options[bs.selectedIndex].text.replace(/ \(\d+\)$/, '') : bid);
    }
    var fdoc = bid === 'all' ? doc : _filterByBucket(doc, blabel);
    renderProof(fdoc.proof || []);
    renderRate(fdoc.rate || []);
    renderSwapOutcomes(fdoc.swap_outcomes || null, fdoc.churn || {});
    renderRecentProofs(fdoc.recent_proofs || []);
    window.__filtered_doc = fdoc;
    renderSwaps();
  }

  function _safeRender(label, fn) {
    try {
      fn();
    } catch (e) {
      console.error('[render] ' + label + ':', e);
    }
  }

  function _syncBucketDropdown(doc) {
    var _bs = $('bucket');
    var _prevVal = _bs ? _bs.value : null;
    _safeRender('buckets', function() { renderBuckets(doc.buckets || []); });
    _bs = $('bucket');
    if (_bs && _bs.options.length > 0 && _bs.options[0].value !== 'all') {
      _bs.add(new Option('All Buckets', 'all'), 0);
    }
    if (_bs && _prevVal) { _bs.value = _prevVal; }
    else if (_bs) { _bs.value = 'all'; }
    if (_bs && !_bs._refilterHooked) {
      _bs.addEventListener('change', _reFilterPanels);
      _bs._refilterHooked = true;
    }
  }

  function _currentBucketLabel() {
    var _bid = $('bucket') ? $('bucket').value : 'all';
    if (_bid === 'all') return {bid: 'all', label: 'all'};
    var _bs2 = $('bucket');
    var _blabel = (window.__labels && window.__labels[_bid]) ||
      (_bs2 && _bs2.selectedIndex >= 0 ? _bs2.options[_bs2.selectedIndex].text.replace(/ \(\d+\)$/, '') : _bid);
    return {bid: _bid, label: _blabel};
  }

  function _renderFilteredPanels(doc) {
    var bk = _currentBucketLabel();
    var _fdoc = bk.bid === 'all' ? doc : _filterByBucket(doc, bk.label);
    _safeRender('proof', function() { renderProof(_fdoc.proof || []); });
    _safeRender('rate', function() { renderRate(_fdoc.rate || []); });
    _safeRender('swap_outcomes', function() { renderSwapOutcomes(_fdoc.swap_outcomes || null, _fdoc.churn || {}); });
    _safeRender('recent_proofs', function() { renderRecentProofs(_fdoc.recent_proofs || []); });
  }

  function _fetchAndApplyLabels() {
    fetch('/api/labels', {cache: 'no-store'}).then(function(r) { return r.json(); }).then(function(ld) {
      window.__labels = ld.labels || {};
      var bs = $('bucket');
      if (bs && bs.options) {
        for (var i = 0; i < bs.options.length; i++) {
          var ip = bs.options[i].value;
          var label = window.__labels[ip];
          if (!label && ip.indexOf("v6:") === 0) {
            var hex = ip.substring(3);
            if (hex.length >= 8) {
              var ip6 = hex.substring(0,4) + ":" + hex.substring(4,8) + "::";
              label = window.__labels[ip6];
            }
          }
          if (label) {
            var pts = bs.options[i].text.match(/\((\d+)\)/);
            bs.options[i].text = label + ' (' + (pts ? pts[1] : '') + ')';
          }
        }
      }
    }).catch(function() {});
  }

  function renderLiveState(doc) {
    window.__current_doc = doc;
    _safeRender('log_window', function() { renderLogWindow(doc); });
    if (doc.now_mono) SERVER_NOW_MONO = doc.now_mono;
    _safeRender('build', function() { renderBuild(doc.build || {}); });
    _safeRender('system', function() { renderSystem(doc.system || {}); });
    _safeRender('tunables', function() { renderTunables(doc.tunables || []); });
    _syncBucketDropdown(doc);
    state.metricByBucket = doc.metric_by_bucket || {};
    state.bucketLive = doc.bucket_live || {};
    state.recentSwapsByBucket = doc.recent_swaps_by_bucket || null;
    _safeRender('metric_for_bucket', function() { renderMetricForBucket(); });
    _safeRender('recent_swaps_for_bucket', function() { renderRecentSwapsForBucket(); });
    if ($("range") && $("range").value === "1h" && state.bucketDoc) {
      _safeRender('bucket_chart', function() { renderBucket(); });
    }
    _renderFilteredPanels(doc);
    state.lastLiveSwaps = doc.recent_swaps || [];
    renderRecentSwapsForBucket();
    _fetchAndApplyLabels();
  }

  function liveRefresh() {
    fetch("current.json", {cache: "no-store"}).then(function (r) {
      if (!r.ok) throw new Error("current.json: " + r.status);
      return r.json();
    }).then(function (doc) {
      renderLiveState(doc);
    }).catch(function (e) {
      setHTML("lv-build",
              '<div class="placeholder">current.json unavailable: ' +
              esc(e.message) + '</div>');
    });
  }

  // SSE real-time updates with polling fallback.
  // SSE pushes data within 1s of collection; polling fallback (30s)
  // ensures the dashboard still works if SSE crashes.
  var _sseSource = null;
  var _pollFallback = null;

  function startLiveUpdates() {
    if (typeof EventSource !== "undefined") {
      _sseSource = new EventSource("/sse");
      _sseSource.onmessage = function (e) {
        try {
          var doc = JSON.parse(e.data);
          renderLiveState(doc);
          refreshNowCardAndChart();
        } catch (err) {
          console.error("SSE parse error:", err);
        }
      };
      _sseSource.onerror = function () {
        console.log("SSE failed, falling back to polling");
        if (_sseSource) _sseSource.close();
        _sseSource = null;
        startPollingFallback();
      };
      console.log("SSE connected — real-time updates enabled");
      // Also do an immediate fetch so the page loads fast (don't
      // wait for the next SSE push)
      liveRefresh();
    } else {
      startPollingFallback();
    }
  }

  function startPollingFallback() {
    if (_pollFallback) return;
    console.log("Polling fallback active (30s interval)");
    _pollFallback = setInterval(function () {
      liveRefresh();
      refreshNowCardAndChart();
    }, 30000);
  }

  function renderNow() {
    var doc = state.bucketDoc;
    if (!doc) return;
    var bid = $("bucket").value;
    var sub = $("nowbucket"); if (sub) sub.textContent = (window.__labels && window.__labels[bid]) || bid;
    // Live data from current.json (not stale bucket document)
    var _live = window.__current_doc || {};
    // Build label from the dropdown text (already updated by renderLiveState)
    var _dropdownLabel = bid;
    var _bs2 = $("bucket");
    if (_bs2 && _bs2.options) {
      for (var _j = 0; _j < _bs2.options.length; _j++) {
        if (_bs2.options[_j].value === bid) {
          _dropdownLabel = _bs2.options[_j].text.replace(/ \(\d+\)$/, '');
          break;
        }
      }
    }
    var _bidLabel = (window.__labels && window.__labels[bid]) || _dropdownLabel || bid;
    // Find this bucket in doc.buckets (for instances, ref_rate, min_rtt, best_alg)
    var _bkt = null;
    var _bidDisplay = shortAddr(bid);
    (_live.buckets || []).forEach(function(b) {
      if (b.dest === bid || b.dest === _bidDisplay || b.dest === _bidLabel) _bkt = b;
    });
    var L = {
      instances: _bkt ? _bkt.inst : null,
      ref_rate: _bkt ? _bkt.ref_mbps : null,
      min_rtt: _bkt ? _bkt.rtt_us : null,
      best_alg: _bkt ? _bkt.best_alg : null,
      collected_ts: _live.generated_ts,
      tcp_rmem_max: (doc.last || {}).tcp_rmem_max || null,
      re: {}
    };
    // Build rate_ema dict from metric_by_bucket (live, 100KB/s units)
    var _mb = _live.metric_by_bucket || {};
    var _mbRows = _mb[bid] || _mb[_bidLabel] || [];
    _mbRows.forEach(function(r) {
      if (r.alg && r.rate_ema != null) L.re[r.alg] = r.rate_ema;
    });
    var setT = function (id, s) { var e = $(id); if (e) e.textContent = s; };
    setT("n_inst", fmtN(L.instances));
    setT("n_ref",  (L.ref_rate != null ? L.ref_rate.toFixed(1) : "0.0") + " Mb/s");
    setT("n_rtt",  fmtRTT(L.min_rtt));
    // Use doc.live_leaders (same source as the swap target leaderboard)
    var _ll = (window.__current_doc && window.__current_doc.live_leaders) || [];
    var _top = null;
    for (var _i = 0; _i < _ll.length; _i++) {
      if (_ll[_i].dest === bid || (_ll[_i].dest === ((window.__labels||{})[bid]))) {
        _top = (_ll[_i].top || [])[0] || null; break;
      }
    }
    // Fallback: use metricByBucket if live_leaders doesn't have this bucket
    if (!_top) {
      var _byB = state.metricByBucket || {};
      var _rows2 = (bid && _byB[bid]) ? _byB[bid] : [];
      _top = _rows2[0] || null;
    }
    // Show picker's choice as best algorithm
    setT("n_best", _top ? _top.alg : (L.best_alg || "-"));
    // Streak/penalty (from live_leaders — has correct bad/null fields)
    if (_top) {
      var _bad = _top.bad || 0;
      var _nul = _top.null || 0;
      var _pen = 16 + _bad * 4 + _nul * 2;
      var _st = (16/_pen).toFixed(3);
      if (_bad > 0) _st += " b=" + _bad;
      if (_nul > 0) _st += " n=" + _nul;
      var _st = (16/_pen).toFixed(3);
      if (_bad > 0) _st += " b=" + _bad;
      if (_nul > 0) _st += " n=" + _nul;
      setT("n_streak", _st);
      setT("n_swaps", _top.count ? fmtN(_top.count) : "-");
      setT("n_algs2", ((state.metricByBucket||{})[bid]||[]).length + "/16");
    } else {
      setT("n_streak", "-");
      setT("n_swaps", "-");
      setT("n_algs2", "-");
    }
    setT("nowupdated",
         L.collected_ts ? "updated " + relTime(L.collected_ts) : "");

    var algs = state.meta.algs;
    var rates = [];
    for (var i = 0; i < algs.length; i++) {
      var a = algs[i];
      var v = (L.re && L.re[a] != null) ? L.re[a] : null;
      if (v != null && v > 0) rates.push({a: a, v: v});
    }
    rates.sort(function (x, y) { return y.v - x.v; });

    if (rates.length) {
      setT("n_rbest", rates[0].a + "  " + fmtRe(rates[0].v) + " Mb/s");
      var line = rates.slice(0, 8).map(function (x) {
        return x.a + " " + fmtRe(x.v);
      }).join("  ·  ");
      setT("n_rates", line);
    } else {
      setT("n_rbest", "-");
      setT("n_rates", "(no live rates for this bucket)");
    }
  }

  var state  = { meta: null, bucketDoc: null, swaps: null, fleet: null, metricByBucket: null, bucketLive: {}, recentSwapsByBucket: null };
  var charts = {};

  function mk(id, cfg) {
    if (charts[id]) { charts[id].destroy(); }
    var cv = $(id);
    if (!cv) return;
    charts[id] = new Chart(cv, cfg);
  }

  function j(url) {
    return fetch(url, {cache: "no-store"}).then(function (r) {
      if (!r.ok) { throw new Error(url + ": " + r.status); }
      return r.json();
    });
  }

  function lineData(cols, series, ts, colors, scale, pointRadius) {
    scale = scale || function (v) { return v; };
    var pr = (pointRadius == null) ? 0 : pointRadius;
    return cols.map(function (c, i) {
      return {
        label: c,
        data: (series[c] || []).map(function (y, k) {
          return {x: ts[k] * 1000, y: y == null ? null : scale(y)};
        }),
        borderColor: colors[i % colors.length],
        backgroundColor: colors[i % colors.length],
        pointRadius: pr,
        pointHoverRadius: Math.max(3, pr + 1),
        borderWidth: 1.5,
        tension: 0.15,
        spanGaps: true,
      };
    });
  }

  function timeOpts(extra) {
    var base = {
      responsive: true,
      maintainAspectRatio: false,
      animation: false,
      interaction: {mode: "nearest", intersect: false},
      layout: {padding: {top: 4, right: 8, bottom: 0, left: 0}},
      scales: {
        x: {
          type: "time",
          time: {tooltipFormat: "MMM d, HH:mm"},
          grid: {display: false},
          ticks: {maxRotation: 0, autoSkipPadding: 24, padding: 4},
        },
        y: {
          beginAtZero: false,
          grid: {drawTicks: false},
          ticks: {maxTicksLimit: 5, padding: 6},
        },
      },
      plugins: {
        legend: {display: false},
        tooltip: {displayColors: true, boxPadding: 4},
      },
    };
    return Object.assign(base, extra || {});
  }

  function renderBucket() {
    var doc = state.bucketDoc, algs = state.meta.algs;
    var rng = $("range").value;
    var s = doc.series[rng];
    var ts = s.ts;
    // 0.4.79: for the 1h range, prefer the CLI-provided ring
    // (60s freshness) for all four series.  bucket_live now
    // carries re_/ss_/bs_/ns_, so the same source serves the
    // rate, score, and streak charts.  Other ranges still load
    // the 15-minute renderer output on demand.
    var bid = $("bucket") ? $("bucket").value : null;
    if (rng === "1h" && bid && state.bucketLive && state.bucketLive[bid]) {
      var lb = state.bucketLive[bid];
      if (lb.ts && lb.ts.length) {
        s = {};
        for (var k in (lb.cols || {})) s[k] = lb.cols[k];
        ts = lb.ts;
      }
    }
    // Expose ts to renderSwaps so the swaps-per-bin x-axis matches the rate /
    // score / streak charts exactly (those use this same ts array).
    window.__bucket_ts = ts && ts.length ? ts.slice() : null;

    function makeSeries(prefix, source) {
      source = source || s;
      return algs.map(function (a) {
        return prefix + a;
      }).filter(function (c) { return c in source; });
    }

    var rateCols = makeSeries("re_", s);
    mk("rate", {
      type: "line",
      data: {datasets: lineData(rateCols, s, ts, PALETTE, scaleRe, 0)},
      options: timeOpts({
        plugins: {
          legend: {
            display: true, position: "bottom", align: "start",
            labels: {boxWidth: 8, boxHeight: 8, padding: 8,
                     font: {size: 10.5}},
          },
        },
      }),
    });

    var ssCols = makeSeries("ss_");
    mk("sscore", {
      type: "line",
      data: {datasets: lineData(ssCols, s, ts, PALETTE, null, 0)},
      options: timeOpts({
        scales: {
          x: {type: "time", time: {tooltipFormat: "MMM d, HH:mm"},
              grid: {display: false},
              ticks: {maxRotation: 0, autoSkipPadding: 24, padding: 4}},
          y: {beginAtZero: false, grid: {drawTicks: false},
              ticks: {maxTicksLimit: 6, padding: 6}},
        },
        plugins: {
          legend: {
            display: true, position: "bottom", align: "start",
            labels: {boxWidth: 8, boxHeight: 8, padding: 8,
                     font: {size: 10.5}},
          },
        },
      }),
    });

    var bsCols = makeSeries("bs_");
    var nsCols = makeSeries("ns_");
    var streakSets = [];
    streakSets = streakSets.concat(
      lineData(bsCols, s, ts, PALETTE, null, 0).map(function (ds) {
        ds.borderDash = [4, 3];
        ds.label = ds.label.replace(/^bs_/, "") + " bad";
        return ds;
      }));
    streakSets = streakSets.concat(
      lineData(nsCols, s, ts, PALETTE, null, 0).map(function (ds) {
        ds.label = ds.label.replace(/^ns_/, "") + " null";
        return ds;
      }));
    mk("streaks", {
      type: "line",
      data: {datasets: streakSets},
      options: timeOpts({
        scales: {
          x: {type: "time", time: {tooltipFormat: "MMM d, HH:mm"},
              grid: {display: false},
              ticks: {maxRotation: 0, autoSkipPadding: 24, padding: 4}},
          y: {beginAtZero: true, grid: {drawTicks: false},
              ticks: {maxTicksLimit: 6, padding: 6, precision: 0}},
        },
        plugins: {
          legend: {
            display: true, position: "bottom", align: "start",
            labels: {boxWidth: 8, boxHeight: 8, padding: 8,
                     font: {size: 10.5}},
          },
        },
      }),
    });
  }

  function renderDivChart(canvasId, suffix) {
    var doc = state.swaps, rng = $("range").value;
    var d = doc[rng];
    if (!d) return;
    var ts = d.ts;
    var sfx = suffix || "";

    function mkLine(key, label, color, dash) {
      return {
        label: label,
        data: (d[key] || []).map(function (y, k) {
          return {x: ts[k] * 1000, y: y};
        }),
        borderColor: color,
        backgroundColor: color,
        borderDash: dash || [],
        pointRadius: 2,
        pointHoverRadius: 4,
        borderWidth: 1.5,
        spanGaps: true,
      };
    }

    mk(canvasId, {
      type: "line",
      data: {datasets: [
        mkLine("d1_rate" + sfx, "diverges=1", "#59a14f"),
        mkLine("d1_lo"   + sfx, "d1 95% lo", "#59a14f", [4, 3]),
        mkLine("d1_hi"   + sfx, "d1 95% hi", "#59a14f", [4, 3]),
        mkLine("d0_rate" + sfx, "diverges=0", "#e15759"),
        mkLine("d0_lo"   + sfx, "d0 95% lo", "#e15759", [4, 3]),
        mkLine("d0_hi"   + sfx, "d0 95% hi", "#e15759", [4, 3]),
      ]},
      options: timeOpts({
        scales: {
          x: {type: "time", grid: {display: false},
              ticks: {maxRotation: 0, autoSkipPadding: 24}},
          y: {min: 0, max: 1, grid: {drawTicks: false},
              ticks: {maxTicksLimit: 5, padding: 6,
                      callback: function (v) {
                        return Math.round(v * 100) + "%";
                      }}},
        },
        plugins: {
          legend: {
            display: true, position: "bottom", align: "start",
            labels: {boxWidth: 8, boxHeight: 8, padding: 8,
                     font: {size: 10.5},
                     filter: function (item) {
                       return !/_lo|_hi/.test(item.text);
                     }},
          },
        },
      }),
    });
  }

  function sustainedHasData() {
    var doc = state.swaps;
    if (!doc) return false;
    var d = doc["24h"] || doc["7d"] || doc["all"];
    if (!d) return false;
    var arr = d.d1_n_sustained || [];
    for (var i = 0; i < arr.length; i++) {
      if ((arr[i] || 0) > 0) return true;
    }
    return false;
  }

  function renderSwaps() {
    var rng = $("range").value;
    var d = null;
    var sse = window.__filtered_doc || window.__current_doc;
    if (sse && sse.swap_outcomes && sse.swap_outcomes.swaps_list) {
      var sw = sse.swap_outcomes.swaps_list;
      var now = (sse.generated_ts || (Date.now()/1000));
      var rSec = {"1h":3600,"24h":86400,"7d":604800,"all":999999999}[rng] || 3600;
      var bSec = {"1h":60,"24h":3600,"7d":21600,"all":86400}[rng] || 60;
      var bins = {};
      sw.forEach(function(s) {
        var t = s.ts || 0;
        if (t >= now - rSec) { var b = Math.floor(t/bSec)*bSec; bins[b] = (bins[b]||0)+1; }
      });
      // Use the SAME ts array as Rate EMA / Swap Score / Bad Streak (exposed via
      // window.__bucket_ts from renderBucket) so all four charts share an identical
      // x-axis, tick-for-tick. Each swap is assigned to its nearest sample timestamp
      // (no bSec binning, no Math.floor snap — that was causing a 1-bin left-shift
      // because rate_ema samples land at ~:30 past each minute, not at :00).
      // Fallback (no bucket data yet): bSec-binned axis spanning (now - rSec, now).
      // "all" range: keep only-populated bins (avoid millions of empties).
      var tsRange = window.__bucket_ts;
      var sk, swapCounts;
      if (rng === "all") {
        sk = Object.keys(bins).map(Number).sort(function(a,b){return a-b;});
        swapCounts = sk.map(function(t){return bins[t] || 0;});
      } else if (tsRange && tsRange.length) {
        // Use tsRange directly as the bin labels — same array the other charts use.
        sk = tsRange.slice().sort(function(a,b){return a-b;});
        swapCounts = sk.map(function(){return 0;});
        // Assign each swap to its nearest ts in sk (binary search; sk is small,
        // typically 25-300 entries for 1h-7d ranges).
        sw.forEach(function(s) {
          var t = s.ts || 0;
          if (t <= sk[0])              { swapCounts[0]++; return; }
          if (t >= sk[sk.length - 1]) { swapCounts[sk.length - 1]++; return; }
          var lo = 0, hi = sk.length - 1;
          while (lo < hi - 1) {
            var mid = (lo + hi) >> 1;
            if (sk[mid] <= t) lo = mid; else hi = mid;
          }
          if (Math.abs(t - sk[lo]) <= Math.abs(t - sk[hi])) {
            swapCounts[lo]++;
          } else {
            swapCounts[hi]++;
          }
        });
      } else {
        var endBin   = Math.floor(now / bSec) * bSec;
        var startBin = Math.floor((now - rSec) / bSec) * bSec;
        sk = [];
        for (var b = startBin; b <= endBin; b += bSec) {
          sk.push(b);
        }
        swapCounts = sk.map(function(t){return bins[t] || 0;});
      }
      if (sk.length) d = {ts: sk, swaps: swapCounts};
    }
    if (!d) { var doc = state.swaps; d = doc ? doc[rng] : null; }
    if (!d) return;
    var ts = d.ts;

    mk("swaps", {
      type: "bar",
      data: {
        labels: ts.map(function (t) { return new Date(t * 1000); }),
        datasets: [{
          label: "swaps",
          data: d.swaps,
          backgroundColor: "#4e79a7",
          borderColor: "#4e79a7",
          borderRadius: 2,
          maxBarThickness: 14,
        }],
      },
      options: timeOpts({
        scales: {
          x: {type: "time", grid: {display: false}, ticks: {maxRotation: 0}},
          y: {beginAtZero: true, grid: {drawTicks: false},
              ticks: {maxTicksLimit: 4, padding: 6}},
        },
      }),
    });
  }

  function renderDivergenceCharts() {
    renderDivChart("div", "");
    renderDivChart("div_sustained", "_sustained");
    var note = document.getElementById("div_sustained_note");
    if (note) {
      if (sustainedHasData()) {
        note.textContent = "sustained = median srate in [t+60, t+300], " +
          "compared to last srate before the swap. Excludes the cwnd-reset " +
          "dip. This is the accurate throughput measure.";
        note.style.color = "";
      } else {
        note.textContent = "no sustained data yet. Requires 0.4.53+ emitting " +
          "`srate` lines AND the collector writing srate.csv AND a swap " +
          "landing with 60+ s of post-swap votes. Will populate on its own.";
        note.style.color = "#9aa0a6";
      }
    }
  }

  function renderFleet() {
    /* 0.4.78.2: fleet.json no longer drives a separate chart.  Its
     * coverage_24h feeds the new coverage column on the top-
     * destination-buckets table.  refreshAll calls this right
     * after assigning state.fleet, so re-render the buckets tail
     * here and the column will fill in. */
    if (state.lastBucketRows) renderBuckets(state.lastBucketRows);
  }

  function loadBucket(id) {
    // "All Buckets" view uses live aggregate data (current.json via
    // renderLiveState), not a historical bucket_*.json file.  Skip the
    // fetch entirely — otherwise we hit a 404 on data/bucket_all.json
    // and renderBucket() throws on null doc.series.
    if (id === "all") {
      state.bucketDoc = null;
      renderNow();
      renderRecentSwapsForBucket();
      renderMetricForBucket();
      return Promise.resolve();
    }
    // 0.4.78.1: sanitize the same way the renderer did when it
    // wrote the file -- bucket ids like "v6:XXXXXXXX" become
    // "v6_XXXXXXXX" on disk.
    var safe = (id || "").replace(/[^A-Za-z0-9._-]/g, "_");
    return j("data/bucket_" + safe.replace(/:/g, "_") + ".json").then(function (doc) {
      state.bucketDoc = doc;
      renderBucket();
      renderNow();
      renderRecentSwapsForBucket();
      renderMetricForBucket();
    });
  }

  function refreshNowCardAndChart() {
    var id = $("bucket").value;
    if (id) loadBucket(id);
  }

function _populateBucketSelect(desiredBucket) {
  var bs = $("bucket");
  var html = "";
  var stillThere = false;
  for (var k = 0; k < state.meta.buckets.length; k++) {
    var b = state.meta.buckets[k];
    html += '<option value="' + b.id + '">' + b.id +
            ' (' + b.points + ')</option>';
    if (b.id === desiredBucket) stillThere = true;
  }
  bs.innerHTML = html;
  bs.add(new Option('All Buckets', 'all'), 0);
  bs.value = stillThere ? desiredBucket : 'all';
  if (window.__current_doc) _reFilterPanels();
  return bs.value;
}

  function refreshAll() {
    var keep = $("bucket").value;
    Promise.all([
      j("data/meta.json"),
      j("data/swaps.json"),
      j("data/fleet.json"),
    ]).then(function (results) {
      state.meta  = results[0];
      state.swaps = results[1];
      state.fleet = results[2];

      var finalBucket = _populateBucketSelect(keep);

      var stamp = new Date(state.meta.generated_ts * 1000).toISOString()
                      .replace("T", " ").slice(0, 19) + "Z";
      if (footgen) footgen.textContent = "rendered " + stamp;
      status("updated " + relTime(state.meta.generated_ts));

      return loadBucket(finalBucket);
    }).then(function () {
      renderSwaps();
      renderFleet();
    }).catch(function (e) {
      err("refresh: " + (e && e.message ? e.message : e), e);
    });
  }

  function boot() {
    startLiveUpdates();

    setInterval(refreshAll, 300000);

    status("loading charts\u2026");
    loadScript("https://cdn.jsdelivr.net/npm/chart.js@4.4.1/dist/chart.umd.min.js")
      .then(function () {
        return loadScript("https://cdn.jsdelivr.net/npm/chartjs-adapter-date-fns@3.0.0/dist/chartjs-adapter-date-fns.bundle.min.js");
      })
      .then(function () {
        applyChartDefaults();
        status("loading data\u2026");
        return Promise.all([
          j("data/meta.json"),
          j("data/swaps.json"),
          j("data/fleet.json"),
        ]);
      })
      .then(function (results) {
        state.meta  = results[0];
        state.swaps = results[1];
        state.fleet = results[2];

        var saved_bucket = null;
        try { saved_bucket = localStorage.getItem("bpftune.bucket"); } catch (e) {}
        _populateBucketSelect(saved_bucket);

        var rs = $("range");
        var rhtml = "";
        for (var m = 0; m < state.meta.ranges.length; m++) {
          rhtml += '<option value="' + state.meta.ranges[m] + '">' +
                   state.meta.ranges[m] + '</option>';
        }
        rs.innerHTML = rhtml;
        /* 0.4.79: default to 1h so the first paint reads the
         * live ring (current.json, 60s) instead of loading a
         * 260 KB bucket_*.json just to show the page.  Restore
         * the user's last choice if there is one; historical
         * ranges load on demand. */
        var saved_range = null;
        try { saved_range = localStorage.getItem("bpftune.range"); } catch (e) {}
        rs.value = (saved_range && state.meta.ranges.indexOf(saved_range) >= 0)
                   ? saved_range : "1h";
        rs.onchange = function () {
          try { localStorage.setItem("bpftune.range", rs.value); } catch (e) {}
          renderBucket();
        };

        var stamp = new Date(state.meta.generated_ts * 1000).toISOString()
                        .replace("T", " ").slice(0, 19) + "Z";
        status("updated " + relTime(state.meta.generated_ts));
        if (footgen) footgen.textContent = "rendered " + stamp;

        var bs = $("bucket");
        bs.onchange = function () {
          try { localStorage.setItem("bpftune.bucket", bs.value); } catch (e) {}
          loadBucket(bs.value);
          renderMetricForBucket();
          renderRecentSwapsForBucket();
        };
        try { localStorage.setItem("bpftune.bucket", bs.value); } catch (e) {}
        rs.onchange = function () {
          renderBucket();
          renderSwaps();
        };

        /* 0.4.79 fix: load the bucket the dropdown actually shows
         * (bs.value), not meta's default.  Boot was setting bs.value
         * to the saved bucket and then loading a different one, so
         * the chart showed one bucket's data under another's name
         * until the user re-selected. */
        if (bs.value === 'all') {
          return Promise.resolve();
        }
        return loadBucket(bs.value);
      })
      .then(function () {
        renderSwaps();
        renderFleet();
      })
      .catch(function (e) {
        err("FAIL: " + (e && e.message ? e.message : e), e);
      });
  }

  boot();

  // 0.4.78.2: dark/light toggle.  Persists per-browser in
  // localStorage; falls back to prefers-color-scheme.
  (function () {
    var saved = null;
    try { saved = localStorage.getItem("bpftune.theme"); } catch (e) {}
    if (saved === "dark" || saved === "light") {
      document.documentElement.setAttribute("data-theme", saved);
    }
    var b = document.getElementById("theme-toggle");
    if (!b) return;
    b.addEventListener("click", function () {
      var cur = document.documentElement.getAttribute("data-theme");
      if (!cur) {
        cur = window.matchMedia(
          "(prefers-color-scheme: dark)").matches ? "dark" : "light";
      }
      var next = cur === "dark" ? "light" : "dark";
      document.documentElement.setAttribute("data-theme", next);
      try { localStorage.setItem("bpftune.theme", next); } catch (e) {}
    });
  })();
})();

  /* ---- Label Editor Modal (reads /etc/bpftune/aliases groups) ---- */
  function _le_esc(s) { return String(s==null?'':s).replace(/[&<>"']/g, function(c) { return ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'})[c]; }); }
  function openLabelEditor() {
    fetch('/api/labels').then(function(r) { return r.json(); }).then(function(d) {
      _le_render(d.labels || {}, d.groups || {});
      document.getElementById('label-modal').style.display = 'flex';
      loadBucketIps();
    }).catch(function(err) { alert('Error: ' + err); });
  }
  function closeLabelEditor() { document.getElementById('label-modal').style.display = 'none'; }
  function _le_render(labels) {
    // Group IPs by label (labels.json is the single source of truth)
    var allLabels = {};
    Object.keys(labels).forEach(function(ip) {
      var label = labels[ip];
      if (!allLabels[label]) allLabels[label] = { ips: [] };
      if (allLabels[label].ips.indexOf(ip) < 0) allLabels[label].ips.push(ip);
    });
    // Render
    var html = '';
    Object.keys(allLabels).sort().forEach(function(label) {
      var g = allLabels[label];
      var count = g.ips.length;
      var ipText = count > 1 ? (count + ' IPs') : g.ips[0];
      html += '<tr><td><input type="text" value="' + _le_esc(label) + '" data-old-label="' + _le_esc(label) + '" class="label-edit-input" data-ips=' + JSON.stringify(g.ips).replace(/"/g,'&quot;') + ' style="font-size:11px;border:1px solid var(--muted);padding:2px 6px;border-radius:3px;width:100%;box-sizing:border-box"></td>' +
        '<td><span style="color:var(--muted)">' + _le_esc(ipText) + '</span>' +
        (count > 1 ? ' <button id="le-btn-' + _le_esc(label) + '" onclick="_le_toggle(\'' + _le_esc(label) + '\')" style="font-size:10px;padding:0 4px;cursor:pointer">show</button>' : '') +
        '</td><td></td></tr>';
      // Show individual IPs with delete buttons
      g.ips.forEach(function(ip, idx) {
        var style = count > 1 ? ' style="display:none"' : '';
        html += '<tr class="le-ips-' + _le_esc(label) + '"' + style + '><td colspan="2" style="padding-left:24px;font-size:11px">' +
          '<span>' + _le_esc(ip) + '</span>' +
          ' <button onclick="_le_del_ip(\'' + _le_esc(ip) + '\')" style="font-size:10px;padding:0 4px;border:1px solid var(--bad);border-radius:3px;cursor:pointer;color:var(--bad);margin-left:6px">x</button>' +
          '</td><td></td></tr>';
      });
    });
    document.getElementById('label-rows').innerHTML = html;
  }
  function addLabel() {
    var raw = document.getElementById('new-ip').value.trim();
    var label = document.getElementById('new-label').value.trim();
    if (!raw || !label) return;
    var ips = raw.split(/[\s,]+/).filter(function(x) { return x.length > 0; });
    ips = ips.map(function(ip) { return ip.split('/')[0]; });
    if (ips.length === 0) return;
    if (ips.length === 1) {
      saveLabel(ips[0], label);
    } else {
      fetch('/api/labels', {method: 'POST', headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({ips: ips, label: label})})
        .then(function(r) { return r.json(); })
        .then(function(d) {
          if (d.ok) {
            _le_render(d.labels || {}, d.groups || {});
            if (window.__liveFetch) window.__liveFetch();
          } else {
            alert('Error: ' + JSON.stringify(d));
          }
        })
        .catch(function(err) { alert('Fetch error: ' + err); });
    }
    document.getElementById('new-ip').value = '';
    document.getElementById('new-label').value = '';
  }

  function _le_toggle(label) {
    var rows = document.querySelectorAll('.le-ips-' + label);
    if (rows.length === 0) return;
    var isHidden = rows[0].style.display === 'none';
    for (var i = 0; i < rows.length; i++) {
      rows[i].style.display = isHidden ? '' : 'none';
    }
    var btn = document.getElementById('le-btn-' + label);
    if (btn) btn.textContent = isHidden ? 'hide' : 'show';
  }
  function _le_del_ip(ip) {
    if (!confirm('Remove ' + ip + '?')) return;
    fetch('/api/labels', {method:'POST', headers:{'Content-Type':'application/json'},
      body: JSON.stringify({ip: ip, label: ''})})
      .then(function(r) { return r.json(); })
      .then(function(d) { _le_render(d.labels||{}); if (window.__liveFetch) window.__liveFetch(); });
  }
  function saveLabel(ip, label) {
    fetch('/api/labels', {method:'POST', headers:{'Content-Type':'application/json'},
      body: JSON.stringify({ip: ip, label: label})})
      .then(function(r) { return r.json(); })
      .then(function(d) { _le_render(d.labels||{}); if (window.__liveFetch) window.__liveFetch(); });
  }

  document.addEventListener('blur', function(e) {
    if (e.target && e.target.classList && e.target.classList.contains('label-edit-input')) {
      var newLabel = e.target.value.trim();
      var oldLabel = e.target.getAttribute('data-old-label');
      if (newLabel && newLabel !== oldLabel) {
        // Rename: delete old, add new for each IP
        var ips = JSON.parse(e.target.getAttribute('data-ips') || '[]');
        ips.forEach(function(ip) {
          fetch('/api/labels', {method:'POST', headers:{'Content-Type':'application/json'},
            body: JSON.stringify({ip: ip, label: ''})});
        });
        setTimeout(function() {
          ips.forEach(function(ip) {
            fetch('/api/labels', {method:'POST', headers:{'Content-Type':'application/json'},
              body: JSON.stringify({ip: ip, label: newLabel})});
          });
          setTimeout(function() {
            fetch('/api/labels').then(function(r){return r.json()}).then(function(d){
              _le_render(d.labels||{});
              if (window.__liveFetch) window.__liveFetch();
            });
          }, 300);
        }, 300);
      }
    }
  }, true);
  document.addEventListener('click', function(e) {
    if (e.target && e.target.id === 'label-modal') closeLabelEditor();
  });

  /* ---- Bucket IPs (individual IPs inside masked buckets) ---- */
  var _bucketIps = null;
  var _allLabels = [];


  function _bi_toggle(masked) {
    var el = document.getElementById('bi-' + masked);
    if (el) el.style.display = (el.style.display === 'none') ? 'block' : 'none';
  }
  function _bi_move(ip, fromBucket, target) {
    if (!target) return;
    if (target === '__new__') {
      target = prompt('New label for ' + ip + ':');
      if (!target) return;
    }
    if (!confirm('Move ' + ip + ' from ' + fromBucket + ' to ' + target + '?')) return;
    // Add the IP to the target group (writes to /etc/bpftune/aliases)
    fetch('/api/labels', {method:'POST', headers:{'Content-Type':'application/json'},
      body: JSON.stringify({ip: ip, label: target})})
      .then(function(r) { return r.json(); })
      .then(function(d) {
        // Reload everything
        loadBucketIps();
        _le_render(d.labels || {}, d.groups || {});
        if (window.__liveFetch) window.__liveFetch();
      });
  }

