#!/bin/sh
# collect_wsl.sh — WSL/host + Docker container metrics every 30s
# Deploy: /srv/metrics/collect_wsl.sh (also runs as container ai2-metrics-collector)

set -eu

OUT_DIR="${METRICS_DIR:-/srv/metrics}"
WSL_CSV="$OUT_DIR/wsl_host.csv"
DOCKER_CSV="$OUT_DIR/docker_stats.csv"

# Prefer explicit DOCKER_HOST; otherwise docker CLI uses local socket
if [ -n "${DOCKER_HOST:-}" ]; then
  export DOCKER_HOST
elif [ -S /var/run/docker.sock ]; then
  unset DOCKER_HOST || true
else
  export DOCKER_HOST=tcp://127.0.0.1:2375
fi

mkdir -p "$OUT_DIR"

if [ ! -f "$WSL_CSV" ]; then
    echo "timestamp,load_1m,load_5m,mem_total_mb,mem_avail_mb,mem_used_real_mb,swap_used_mb" > "$WSL_CSV"
fi

if [ ! -f "$DOCKER_CSV" ]; then
    echo "timestamp,container,cpu_perc,mem_usage,mem_perc,block_io" > "$DOCKER_CSV"
fi

while true; do
    TS=$(date '+%Y-%m-%d %H:%M:%S')

    read -r LOAD1 LOAD5 _ < /proc/loadavg
    eval "$(awk '
        /^MemTotal:/ {mt=$2}
        /^MemAvailable:/ {ma=$2}
        /^SwapTotal:/ {st=$2}
        /^SwapFree:/ {sf=$2}
        END {
            printf "MT=%d MA=%d MU=%d SU=%d", mt/1024, ma/1024, (mt-ma)/1024, (st-sf)/1024
        }
    ' /proc/meminfo)"

    echo "$TS,$LOAD1,$LOAD5,$MT,$MA,$MU,$SU" >> "$WSL_CSV"

    docker stats --no-stream --format \
        "$TS,{{.Name}},{{.CPUPerc}},{{.MemUsage}},{{.MemPerc}},{{.BlockIO}}" \
        >> "$DOCKER_CSV" 2>/dev/null || true

    sleep 30
done