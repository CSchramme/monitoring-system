#!/usr/bin/env bash
# Monitoring agent for Linux: reports CPU, memory, swap, disk, load and uptime to a push monitor.
#
# Usage:   linux-agent.sh <push-url>
#          MONITOR_URL=<push-url> linux-agent.sh
# Options (environment):
#   MONITOR_DISK_PATHS  space separated mount points to report (default: "/")
#   MONITOR_LOOP        seconds between reports; if set, the script runs forever (e.g. under systemd)
#
# Typical setup (runs every minute via cron):
#   curl -fsSL <server>/agent/linux-agent.sh -o /usr/local/bin/monitor-agent && chmod +x /usr/local/bin/monitor-agent
#   echo '* * * * * root /usr/local/bin/monitor-agent <push-url> >/dev/null 2>&1' > /etc/cron.d/monitor-agent

set -euo pipefail

URL="${1:-${MONITOR_URL:-}}"
if [ -z "$URL" ]; then
    echo "usage: $0 <push-url>" >&2
    exit 1
fi
DISK_PATHS="${MONITOR_DISK_PATHS:-/}"

cpu_sample() {
    # prints: idle total
    awk '/^cpu /{idle=$5+$6; total=0; for (i=2; i<=NF; i++) total+=$i; print idle, total}' /proc/stat
}

collect() {
    local idle1 total1 idle2 total2
    read -r idle1 total1 < <(cpu_sample)
    sleep 1
    read -r idle2 total2 < <(cpu_sample)

    local cpu mem swap load1 load5 cores uptime procs
    cpu=$(awk -v i1="$idle1" -v t1="$total1" -v i2="$idle2" -v t2="$total2" \
        'BEGIN { dt = t2 - t1; if (dt <= 0) print 0; else printf "%.1f", (1 - (i2 - i1) / dt) * 100 }')
    mem=$(awk '/^MemTotal:/{t=$2} /^MemAvailable:/{a=$2} END { if (t > 0) printf "%.1f", (t - a) / t * 100; else print 0 }' /proc/meminfo)
    swap=$(awk '/^SwapTotal:/{t=$2} /^SwapFree:/{f=$2} END { if (t > 0) printf "%.1f", (t - f) / t * 100; else print 0 }' /proc/meminfo)
    read -r load1 load5 _ < /proc/loadavg
    cores=$(nproc 2>/dev/null || grep -c ^processor /proc/cpuinfo)
    uptime=$(awk '{printf "%d", $1}' /proc/uptime)
    procs=$(ls -d /proc/[0-9]* 2>/dev/null | wc -l)

    local metrics disk_json="" name path pct
    metrics="\"cpu\":$cpu,\"mem\":$mem,\"swap\":$swap,\"load1\":$load1,\"load5\":$load5,\"cores\":$cores,\"uptime\":$uptime,\"procs\":$procs"
    for path in $DISK_PATHS; do
        pct=$(df -P "$path" 2>/dev/null | awk 'NR==2 { gsub("%", "", $5); print $5 }')
        [ -n "$pct" ] || continue
        if [ "$path" = "/" ]; then
            name="disk"
        else
            name="disk_$(echo "$path" | sed 's#^/##; s#[^A-Za-z0-9]#_#g')"
        fi
        disk_json="$disk_json,\"$name\":$pct"
    done

    local host
    host=$(hostname 2>/dev/null || cat /proc/sys/kernel/hostname)
    printf '{"status":"up","message":"%s","metrics":{%s%s}}' "$host" "$metrics" "$disk_json"
}

send() {
    curl -fsS -m 15 --retry 2 -X POST -H 'Content-Type: application/json' -d "$(collect)" "$URL" >/dev/null
}

if [ -n "${MONITOR_LOOP:-}" ]; then
    while true; do
        send || echo "monitor-agent: send failed" >&2
        sleep "$MONITOR_LOOP"
    done
else
    send
fi
