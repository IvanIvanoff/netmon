#!/usr/bin/env bash
#
# netmon.sh
# Run in the background during calls to log network conditions.
# Usage:
#   ./netmon.sh start   # begin logging
#   ./netmon.sh stop    # stop logging
#   ./netmon.sh review  # pretty-print the latest log
#
# Logs are saved to ~/call-network-logs/

set -euo pipefail

LOG_DIR="$HOME/call-network-logs"
PID_FILE="$LOG_DIR/.monitor.pid"
INTERVAL="${MONITOR_INTERVAL:-5}" # seconds between samples

PING_TARGET="${PING_TARGET:-8.8.8.8}"
PING_COUNT=3
AIRPORT_PATH="/System/Library/PrivateFrameworks/Apple80211.framework/Versions/Current/Resources/airport"

mkdir -p "$LOG_DIR"

# ── compile wifi helper for modern macOS (Sonoma+) ──────────────────
WIFI_HELPER="$LOG_DIR/.wifi_helper"
_compile_wifi_helper() {
  [[ -x "$WIFI_HELPER" ]] && return 0 # already compiled
  # Check if airport actually provides data (deprecated on Sequoia+)
  if [[ -x "$AIRPORT_PATH" ]] && "$AIRPORT_PATH" -I 2>/dev/null | grep -q "agrCtlRSSI"; then
    return 0 # airport works, no need for helper
  fi
  command -v swiftc &>/dev/null || return 1
  swiftc -O -o "$WIFI_HELPER" - 2>/dev/null <<'SWIFT'
import CoreWLAN
guard let iface = CWWiFiClient.shared().interface() else { exit(1) }
print("     agrCtlRSSI: \(iface.rssiValue())")
print("     agrCtlNoise: \(iface.noiseMeasurement())")
print("          SSID: \(iface.ssid() ?? "unknown")")
print("       channel: \(iface.wlanChannel()?.channelNumber ?? 0)")
print("     lastTxRate: \(Int(iface.transmitRate()))")
SWIFT
}
_compile_wifi_helper

# ── helpers ──────────────────────────────────────────────────────────

timestamp() { date "+%Y-%m-%d %H:%M:%S"; }

get_wifi_info() {
  # Try 1: airport utility (pre-Sonoma macOS)
  if [[ -x "$AIRPORT_PATH" ]]; then
    local out
    out=$("$AIRPORT_PATH" -I 2>/dev/null)
    # Only use if it actually contains wifi data (not just a deprecation warning)
    if echo "$out" | grep -q "agrCtlRSSI"; then
      echo "$out"
      return
    fi
  fi
  # Try 2: pre-compiled CoreWLAN helper (Sonoma+)
  if [[ -x "$WIFI_HELPER" ]]; then
    "$WIFI_HELPER" 2>/dev/null && return
  fi
  echo "wifi: unavailable"
}

run_ping() {
  # macOS ping -W is in milliseconds (not seconds like Linux)
  ping -c "$PING_COUNT" -W 2000 "$PING_TARGET" 2>/dev/null
}

parse_ping() {
  local output="$1"
  local loss avg min max

  loss=$(echo "$output" | grep -oE '[0-9.]+% packet loss' | grep -oE '[0-9.]+' || echo "?")
  # macOS ping stats line: round-trip min/avg/max/stddev = ...
  local stats_line
  stats_line=$(echo "$output" | grep "round-trip\|rtt" || echo "")
  if [[ -n "$stats_line" ]]; then
    read -r min avg max <<< "$(echo "$stats_line" | awk -F'[/ =]+' '{
      for(i=1;i<=NF;i++) if($i ~ /^[0-9]+\.[0-9]+$/) { n++; v[n]=$i }
      print v[1], v[2], v[3]
    }')"
  else
    min="?"
    avg="?"
    max="?"
  fi

  echo "${loss}|${min:-?}|${avg:-?}|${max:-?}"
}

get_dns_latency() {
  # macOS date doesn't support %N; use python3 for ms-precision timing
  python3 -c "
import time, subprocess
t0 = time.time()
subprocess.run(['nslookup', 'google.com'], capture_output=True, timeout=5)
print(int((time.time() - t0) * 1000))
" 2>/dev/null || echo "?"
}

get_active_interface() {
  route -n get default 2>/dev/null | grep "interface:" | awk '{print $2}' || echo "unknown"
}

get_public_ip() {
  curl -s --max-time 3 https://api.ipify.org 2>/dev/null || echo "?"
}

# ── sampling loop ────────────────────────────────────────────────────

sample_loop() {
  local logfile="$1"
  # Disable errexit/nounset/pipefail — the background loop must not die
  # on transient failures (e.g. grep no-match, network timeout)
  set +e +u +o pipefail

  # Write CSV header
  echo "timestamp,ssid,channel,rssi_dBm,noise_dBm,snr_dB,tx_rate_Mbps,interface,local_ip,public_ip,ping_target,loss_%,ping_min_ms,ping_avg_ms,ping_max_ms,dns_ms" >"$logfile"

  # Grab public IP once at start (doesn't change mid-call usually)
  local pub_ip
  pub_ip=$(get_public_ip)

  local ping_file="/tmp/netmon_ping.$$"
  local dns_file="/tmp/netmon_dns.$$"
  trap 'rm -f "$ping_file" "$dns_file"' EXIT

  while true; do
    local ts wifi_info ssid channel rssi noise snr tx_rate iface lip
    ts=$(timestamp)

    # Wifi info — single call, parse all fields from cached output
    wifi_info=$(get_wifi_info)
    ssid=$(echo "$wifi_info" | grep -i "^ *SSID" | head -1 | awk '{print $NF}')
    [[ -z "$ssid" ]] && ssid=$(networksetup -getairportnetwork en0 2>/dev/null | awk -F': ' '{print $2}')
    ssid="${ssid:-unknown}"
    channel=$(echo "$wifi_info" | grep -i "channel" | head -1 | awk '{print $NF}')
    rssi=$(echo "$wifi_info" | grep -i "agrCtlRSSI\|signal" | head -1 | awk '{print $NF}')
    noise=$(echo "$wifi_info" | grep -i "agrCtlNoise" | head -1 | awk '{print $NF}')
    tx_rate=$(echo "$wifi_info" | grep -i "lastTxRate\|maxRate" | head -1 | awk '{print $NF}')

    # Interface + IP (single get_active_interface call)
    iface=$(get_active_interface)
    lip=$(ifconfig "$iface" 2>/dev/null | grep "inet " | awk '{print $2}' | head -1)
    lip="${lip:-?}"

    # Compute SNR
    if [[ "$rssi" =~ ^-?[0-9]+$ ]] && [[ "$noise" =~ ^-?[0-9]+$ ]]; then
      snr=$((rssi - noise))
    else
      snr="?"
    fi

    # Ping + DNS in parallel
    run_ping > "$ping_file" &
    get_dns_latency > "$dns_file" &
    wait

    local ping_parsed loss pmin pavg pmax dns_ms
    ping_parsed=$(parse_ping "$(cat "$ping_file")")
    IFS='|' read -r loss pmin pavg pmax <<<"$ping_parsed"
    dns_ms=$(cat "$dns_file")
    dns_ms="${dns_ms:-?}"

    echo "${ts},${ssid},${channel},${rssi},${noise},${snr},${tx_rate},${iface},${lip},${pub_ip},${PING_TARGET},${loss},${pmin},${pavg},${pmax},${dns_ms}" >>"$logfile"

    sleep "$INTERVAL"
  done
}

# ── commands ─────────────────────────────────────────────────────────

cmd_start() {
  if [[ -f "$PID_FILE" ]] && kill -0 "$(cat "$PID_FILE")" 2>/dev/null; then
    echo "Monitor already running (PID $(cat "$PID_FILE"))."
    echo "Log: $(ls -t "$LOG_DIR"/call-*.csv 2>/dev/null | head -1)"
    return 0
  fi

  local logfile="$LOG_DIR/call-$(date +%Y%m%d-%H%M%S).csv"
  echo "Starting network monitor..."
  echo "  Log file : $logfile"
  echo "  Interval : ${INTERVAL}s"
  echo "  Ping host: $PING_TARGET"

  sample_loop "$logfile" &
  local pid=$!
  echo "$pid" >"$PID_FILE"
  echo "  PID      : $pid"
  echo ""
  echo "Run './netmon.sh stop' when your call ends."
}

cmd_stop() {
  if [[ ! -f "$PID_FILE" ]]; then
    echo "No monitor running."
    return 0
  fi

  local pid
  pid=$(cat "$PID_FILE")
  if kill -0 "$pid" 2>/dev/null; then
    kill "$pid"
    echo "Stopped monitor (PID $pid)."
  else
    echo "Monitor process $pid already gone."
  fi
  rm -f "$PID_FILE"

  local latest
  latest=$(ls -t "$LOG_DIR"/call-*.csv 2>/dev/null | head -1)
  if [[ -n "$latest" ]]; then
    local samples
    samples=$(($(wc -l <"$latest") - 1))
    echo "Logged $samples samples to: $latest"
  fi
}

cmd_review() {
  local target="${1:-}"

  if [[ -z "$target" ]]; then
    target=$(ls -t "$LOG_DIR"/call-*.csv 2>/dev/null | head -1)
  fi

  if [[ -z "$target" || ! -f "$target" ]]; then
    echo "No log files found in $LOG_DIR"
    return 1
  fi

  local samples
  samples=$(($(wc -l <"$target") - 1))
  local first last
  first=$(sed -n '2p' "$target" | cut -d, -f1)
  last=$(tail -1 "$target" | cut -d, -f1)

  echo "═══════════════════════════════════════════════════════"
  echo " Call Network Report"
  echo "═══════════════════════════════════════════════════════"
  echo " File     : $target"
  echo " Samples  : $samples"
  echo " Period   : $first  →  $last"
  echo ""

  # --- Ping stats ---
  echo "── Ping ($PING_TARGET) ──────────────────────────────"
  awk -F, 'NR>1 && $14 ~ /^[0-9.]/ {
        sum+=$14; n++
        if(n==1 || $14+0 < mn) mn=$14+0
        if(n==1 || $14+0 > mx) mx=$14+0
    } END {
        if(n>0) printf "  Avg latency : %.1f ms\n  Min         : %.1f ms\n  Max         : %.1f ms\n", sum/n, mn, mx
        else print "  No valid ping data"
    }' "$target"

  # Packet loss
  awk -F, 'NR>1 && $12 ~ /^[0-9.]/ {
        sum+=$12; n++
        if($12+0 > 0) bad++
    } END {
        if(n>0) printf "  Avg loss    : %.1f%%\n  Samples w/ loss: %d/%d\n", sum/n, bad+0, n
    }' "$target"
  echo ""

  # --- Wi-Fi signal ---
  echo "── Wi-Fi Signal ───────────────────────────────────────"
  awk -F, 'NR>1 && $4 ~ /^-?[0-9]/ {
        sum+=$4; n++
        if(n==1 || $4+0 < mn) mn=$4+0
        if(n==1 || $4+0 > mx) mx=$4+0
    } END {
        if(n>0) {
            avg=sum/n
            printf "  RSSI avg    : %.0f dBm", avg
            if(avg > -50) printf "  (excellent)\n"
            else if(avg > -60) printf "  (good)\n"
            else if(avg > -70) printf "  (fair)\n"
            else printf "  (weak)\n"
            printf "  RSSI range  : %d to %d dBm\n", mn, mx
        } else print "  No RSSI data"
    }' "$target"

  # SNR
  awk -F, 'NR>1 && $6 ~ /^-?[0-9]/ {
        sum+=$6; n++
    } END {
        if(n>0) {
            avg=sum/n
            printf "  SNR avg     : %.0f dB", avg
            if(avg > 40) printf "  (excellent)\n"
            else if(avg > 25) printf "  (good)\n"
            else if(avg > 15) printf "  (fair)\n"
            else printf "  (poor)\n"
        }
    }' "$target"

  # TX rate
  awk -F, 'NR>1 && $7 ~ /^[0-9]/ {
        sum+=$7; n++
        if(n==1 || $7+0 < mn) mn=$7+0
    } END {
        if(n>0) printf "  TX rate avg : %.0f Mbps (min: %.0f)\n", sum/n, mn
    }' "$target"
  echo ""

  # --- DNS ---
  echo "── DNS Latency ────────────────────────────────────────"
  awk -F, 'NR>1 && $16 ~ /^[0-9]/ {
        sum+=$16; n++
        if(n==1 || $16+0 > mx) mx=$16+0
    } END {
        if(n>0) printf "  Avg         : %.0f ms\n  Max         : %.0f ms\n", sum/n, mx
        else print "  No DNS data"
    }' "$target"
  echo ""

  # --- Problem detection ---
  echo "── Issues Detected ────────────────────────────────────"
  local issues=0

  # High latency spikes
  local spikes
  spikes=$(awk -F, 'NR>1 && $14+0 > 100 {print $1": "$14"ms"}' "$target")
  if [[ -n "$spikes" ]]; then
    echo "  ⚠  High latency spikes (>100ms):"
    echo "$spikes" | head -5 | sed 's/^/       /'
    local spike_count
    spike_count=$(echo "$spikes" | wc -l | tr -d ' ')
    [[ "$spike_count" -gt 5 ]] && echo "       ... and $((spike_count - 5)) more"
    issues=$((issues + 1))
  fi

  # Packet loss
  local loss_events
  loss_events=$(awk -F, 'NR>1 && $12+0 > 0 {print $1": "$12"% loss"}' "$target")
  if [[ -n "$loss_events" ]]; then
    echo "  ⚠  Packet loss events:"
    echo "$loss_events" | head -5 | sed 's/^/       /'
    local loss_count
    loss_count=$(echo "$loss_events" | wc -l | tr -d ' ')
    [[ "$loss_count" -gt 5 ]] && echo "       ... and $((loss_count - 5)) more"
    issues=$((issues + 1))
  fi

  # Weak signal
  local weak_signal
  weak_signal=$(awk -F, 'NR>1 && $4 ~ /^-/ && $4+0 < -75 {print $1": "$4" dBm"}' "$target")
  if [[ -n "$weak_signal" ]]; then
    echo "  ⚠  Weak Wi-Fi signal (<-75 dBm):"
    echo "$weak_signal" | head -5 | sed 's/^/       /'
    issues=$((issues + 1))
  fi

  # Slow DNS
  local slow_dns
  slow_dns=$(awk -F, 'NR>1 && $16+0 > 200 {print $1": "$16"ms"}' "$target")
  if [[ -n "$slow_dns" ]]; then
    echo "  ⚠  Slow DNS lookups (>200ms):"
    echo "$slow_dns" | head -5 | sed 's/^/       /'
    issues=$((issues + 1))
  fi

  if [[ "$issues" -eq 0 ]]; then
    echo "  ✓  No significant issues detected."
  fi

  echo ""
  echo "═══════════════════════════════════════════════════════"
  echo " Raw CSV: $target"
  echo "═══════════════════════════════════════════════════════"
}

cmd_list() {
  echo "Available logs:"
  ls -lt "$LOG_DIR"/call-*.csv 2>/dev/null | awk '{print "  "$NF"  ("$5" bytes, "$6" "$7" "$8")"}' || echo "  (none)"
}

# ── main ─────────────────────────────────────────────────────────────

case "${1:-help}" in
start) cmd_start ;;
stop) cmd_stop ;;
review) cmd_review "${2:-}" ;;
list) cmd_list ;;
help | *)
  echo "netmon.sh — log network conditions during calls"
  echo ""
  echo "Usage:"
  echo "  $0 start           Start monitoring (runs in background)"
  echo "  $0 stop            Stop monitoring"
  echo "  $0 review [file]   Review latest (or specified) log"
  echo "  $0 list            List all log files"
  echo ""
  echo "Environment variables:"
  echo "  MONITOR_INTERVAL   Seconds between samples (default: 10)"
  echo "  PING_TARGET        Host to ping (default: 8.8.8.8)"
  ;;
esac
