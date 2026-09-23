# bpftune-dashboard

Live throughput + swap-outcome dashboard for bpftune.

Ships three scripts (`bpftune-cli.py`, `bpftune-collector.py`,
`bpftune-render.py`), a cron-driven collector + renderer, systemd units
for `trace_pipe` capture and for `python3 -m http.server`, and a
logrotate drop-in. All state under `/var/lib/bpftune/history/`.

## Install

    git clone https://github.com/cddeppe/bpftune
    cd bpftune
    sudo bash dashboard/deploy.sh

Open `http://<host>:8080/`.

## Fleet

    for h in host1 host2 host3; do
        ssh "$h" 'cd ~/bpftune && git pull && sudo bash dashboard/deploy.sh'
    done

Hosts where `bpftool map dump name remote_host_map` fails are skipped
cleanly.

## Options

    sudo bash dashboard/deploy.sh --port 9090
    sudo bash dashboard/deploy.sh --force
    sudo python3 dashboard/install.py --dry-run
    sudo python3 dashboard/install.py --no-verify

## Data

* `buckets.v2.csv` - per-destination-per-minute. Per-alg columns:
  `mv_*`, `re_*`, `ss_*`, `bs_*`, `ns_*`.
* `swaps.csv` - one row per algorithm swap, resolved post-hoc.
* `srate.csv` - raw srate samples keyed by swap cookie.
* `current.json` - snapshot from `bpftune-cli.py --json`, polled by the
  live panel every 30 s.

**Only `collected_ts` is wall-clock.** `boot_ts` is monotonic seconds
from the trace log; never derive a calendar date from it.

## Swap target picker

    score    = rate_ema * (swap_score / 256) * penalty
    penalty  = 16 / (16 + bad_streak*4 + null_streak*2)

`penalty` is 1.0 with no failures; always positive. Streaks are a
multiplier, not a gate. Top row = picker's choice.

## Uninstall

    sudo bash dashboard/scripts/uninstall.sh           # keep history
    sudo bash dashboard/scripts/uninstall.sh --purge   # delete history too
