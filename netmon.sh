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
INTERVAL="${MONITOR_INTERVAL:-2}" # seconds between samples

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

REPORT_WIDTH=140

timestamp() { date "+%Y-%m-%d %H:%M:%S"; }

_section() {
  local label="── $1 "
  local pad=$((REPORT_WIDTH - ${#label}))
  printf "%s" "$label"
  printf '─%.0s' $(seq 1 "$pad")
  echo
}

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
    read -r min avg max <<<"$(echo "$stats_line" | awk -F'[/ =]+' '{
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

_nettop_snapshot() {
  # Raw nettop snapshot: process.pid,bytes_in,bytes_out,rx_dupe,rx_ooo,re-tx
  nettop -P -L 1 -n -x -J time,bytes_in,bytes_out,rx_dupe,rx_ooo,re-tx 2>/dev/null |
    awk -F, 'NR > 1 && ($3+0 > 0 || $4+0 > 0) { print $2","$3","$4","$5","$6","$7 }'
}

capture_traffic() {
  # Compute per-interval deltas against previous snapshot, append to traffic CSV
  # curr_file is pre-populated by the caller; name_file is shared with capture_connections
  local ts="$1" traffic_file="$2" prev_file="$3" curr_file="$4"
  local name_file="${5:-${curr_file}.names}"

  if [[ -s "$prev_file" ]]; then
    awk -F, -v ts="$ts" '
      FILENAME == ARGV[1] { fullname[$1] = $2; next }
      FILENAME == ARGV[2] { prev_in[$1]=$2; prev_out[$1]=$3; prev_dup[$1]=$4; prev_ooo[$1]=$5; prev_retx[$1]=$6; next }
      {
        din  = $2 - (prev_in[$1]+0);  if (din < 0) din = 0
        dout = $3 - (prev_out[$1]+0); if (dout < 0) dout = 0
        ddup = $4 - (prev_dup[$1]+0); if (ddup < 0) ddup = 0
        dooo = $5 - (prev_ooo[$1]+0); if (dooo < 0) dooo = 0
        dretx= $6 - (prev_retx[$1]+0); if (dretx < 0) dretx = 0
        if (din > 0 || dout > 0) {
          proc = $1; pid = ""
          n = split(proc, p, ".")
          if (n > 1 && p[n] ~ /^[0-9]+$/) {
            pid = p[n]; proc = p[1]
            for (i = 2; i < n; i++) proc = proc "." p[i]
          }
          if (pid != "" && pid in fullname) proc = fullname[pid]
          printf "%s,%s,%s,%d,%d,%d,%d,%d\n", ts, proc, pid, din, dout, ddup, dooo, dretx
        }
      }
    ' "$name_file" "$prev_file" "$curr_file" >>"$traffic_file"
  fi

  cp "$curr_file" "$prev_file"
}

_nettop_conn_snapshot() {
  # Connection-level snapshot: process.pid|remote_ip:port,bytes_in,bytes_out,retransmits
  # Parses nettop output grouping connections under their parent process
  nettop -m tcp -L 1 -n -x 2>/dev/null | awk -F, '
    NR == 1 { next }
    # Process summary line (interface field is empty)
    $3 == "" && $2 ~ /\.[0-9]+$/ { proc = $2; next }
    # Connection line with traffic
    $2 ~ /<->/ && ($5+0 > 0 || $6+0 > 0) {
      # Extract remote from "tcp4 local<->remote"
      split($2, lr, "<->")
      remote = lr[2]
      print proc"|"remote","$5","$6","$9
    }
  '
}

capture_connections() {
  local ts="$1" conn_file="$2" prev_file="$3" curr_file="$4" name_file="$5"

  _nettop_conn_snapshot >"$curr_file"

  if [[ -s "$prev_file" ]]; then
    awk -F, -v ts="$ts" '
      FILENAME == ARGV[1] { fullname[$1] = $2; next }
      FILENAME == ARGV[2] { prev_in[$1]=$2; prev_out[$1]=$3; prev_retx[$1]=$4; next }
      {
        din  = $2 - (prev_in[$1]+0);  if (din < 0) din = 0
        dout = $3 - (prev_out[$1]+0); if (dout < 0) dout = 0
        dretx= $4 - (prev_retx[$1]+0); if (dretx < 0) dretx = 0
        if (din > 0 || dout > 0) {
          # key is "process.pid|remote_ip:port" — split on |
          split($1, kp, "|")
          proc_raw = kp[1]; remote = kp[2]
          pid = ""
          n = split(proc_raw, p, ".")
          if (n > 1 && p[n] ~ /^[0-9]+$/) {
            pid = p[n]; proc = p[1]
            for (i = 2; i < n; i++) proc = proc "." p[i]
          } else { proc = proc_raw }
          if (pid != "" && pid in fullname) proc = fullname[pid]
          # Split remote into ip and port
          n = split(remote, rp, ":")
          rport = rp[n]; rip = rp[1]
          for (i = 2; i < n; i++) rip = rip ":" rp[i]
          printf "%s,%s,%s,%s,%s,%d,%d,%d\n", ts, proc, pid, rip, rport, din, dout, dretx
        }
      }
    ' "$name_file" "$prev_file" "$curr_file" >>"$conn_file"
  fi

  cp "$curr_file" "$prev_file"
}

# ── sampling loop ────────────────────────────────────────────────────

sample_loop() {
  local logfile="$1" traffic_file="$2" conn_file="$3"
  # Disable errexit/nounset/pipefail — the background loop must not die
  # on transient failures (e.g. grep no-match, network timeout)
  set +e +u +o pipefail

  # Write CSV headers
  echo "timestamp,ssid,channel,rssi_dBm,noise_dBm,snr_dB,tx_rate_Mbps,interface,local_ip,public_ip,ping_target,loss_%,ping_min_ms,ping_avg_ms,ping_max_ms,dns_ms" >"$logfile"
  echo "sample_ts,process,pid,bytes_in,bytes_out,rx_dupe,rx_ooo,retransmits" >"$traffic_file"
  echo "sample_ts,process,pid,remote_ip,remote_port,bytes_in,bytes_out,retransmits" >"$conn_file"

  # Grab public IP once at start (doesn't change mid-call usually)
  local pub_ip
  pub_ip=$(get_public_ip)

  local ping_file="/tmp/netmon_ping.$$"
  local dns_file="/tmp/netmon_dns.$$"
  local prev_traffic="/tmp/netmon_tprev.$$"
  local curr_traffic="/tmp/netmon_tcurr.$$"
  local prev_conn="/tmp/netmon_cprev.$$"
  local curr_conn="/tmp/netmon_ccurr.$$"
  local name_file="/tmp/netmon_names.$$"
  trap 'rm -f "$ping_file" "$dns_file" "$prev_traffic" "$curr_traffic" "$prev_conn" "$curr_conn" "$name_file"' EXIT

  # Baseline snapshots (not logged — just sets the zero point)
  _nettop_snapshot >"$prev_traffic"
  _nettop_conn_snapshot >"$prev_conn"

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

    # Ping + DNS in parallel while we capture traffic
    run_ping >"$ping_file" &
    get_dns_latency >"$dns_file" &

    # Traffic + connections (sequential — they share name_file)
    _nettop_snapshot >"$curr_traffic"
    # Build PID→name map once, used by both capture functions
    local pids
    pids=$(awk -F, '{ n=split($1,p,"."); if(p[n]~/^[0-9]+$/) print p[n] }' "$curr_traffic" | sort -u | paste -sd, -)
    if [[ -n "$pids" ]]; then
      ps -p "$pids" -o pid=,comm= 2>/dev/null | \
        awk '{ pid=$1; $1=""; sub(/^ +/,""); n=split($0,a,"/"); print pid","a[n] }' >"$name_file"
    else
      : >"$name_file"
    fi
    capture_traffic "$ts" "$traffic_file" "$prev_traffic" "$curr_traffic" "$name_file" &
    capture_connections "$ts" "$conn_file" "$prev_conn" "$curr_conn" "$name_file" &
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

  local stamp
  stamp=$(date +%Y%m%d-%H%M%S)
  local logfile="$LOG_DIR/call-${stamp}.csv"
  local traffic_file="$LOG_DIR/call-${stamp}-traffic.csv"
  local conn_file="$LOG_DIR/call-${stamp}-connections.csv"
  echo "Starting network monitor..."
  echo "  Log file : $logfile"
  echo "  Traffic  : $traffic_file"
  echo "  Connects : $conn_file"
  echo "  Interval : ${INTERVAL}s"
  echo "  Ping host: $PING_TARGET"

  sample_loop "$logfile" "$traffic_file" "$conn_file" &
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
  latest=$(ls -t "$LOG_DIR"/call-*.csv 2>/dev/null | grep -vE '(-traffic|-connections)' | head -1)
  if [[ -n "$latest" ]]; then
    local samples
    samples=$(($(wc -l <"$latest") - 1))
    echo "Logged $samples samples to: $latest"
    local traffic="${latest%.csv}-traffic.csv"
    [[ -f "$traffic" ]] && echo "Traffic log    : $traffic"
    local conns="${latest%.csv}-connections.csv"
    [[ -f "$conns" ]] && echo "Connections log: $conns"
  fi
}

cmd_review() {
  local target="${1:-}"

  if [[ -z "$target" ]]; then
    target=$(ls -t "$LOG_DIR"/call-*.csv 2>/dev/null | grep -vE '(-traffic|-connections)' | head -1)
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

  printf '═%.0s' $(seq 1 "$REPORT_WIDTH"); echo
  echo " Call Network Report"
  printf '═%.0s' $(seq 1 "$REPORT_WIDTH"); echo
  echo " File     : $target"
  echo " Samples  : $samples"
  echo " Period   : $first  →  $last"
  echo ""

  # --- Ping stats ---
  _section "Ping ($PING_TARGET)"
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
  _section "Wi-Fi Signal"
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
  _section "DNS Latency"
  awk -F, 'NR>1 && $16 ~ /^[0-9]/ {
        sum+=$16; n++
        if(n==1 || $16+0 > mx) mx=$16+0
    } END {
        if(n>0) printf "  Avg         : %.0f ms\n  Max         : %.0f ms\n", sum/n, mx
        else print "  No DNS data"
    }' "$target"
  echo ""

  # --- Traffic ---
  local traffic_file="${target%.csv}-traffic.csv"
  if [[ -f "$traffic_file" ]]; then
    _section "Per-Process Traffic"
    # Sum per-interval deltas per process, show top 10 by total bytes
    awk -F, '
      NR == 1 { next }
      { in_sum[$2] += $4; out_sum[$2] += $5; retx_sum[$2] += $8 }
      END {
        for (proc in in_sum) {
          total = in_sum[proc] + out_sum[proc]
          if (total > 0)
            printf "%d|%s|%d|%d|%d\n", total, proc, in_sum[proc], out_sum[proc], retx_sum[proc]
        }
      }
    ' "$traffic_file" | sort -t'|' -k1 -nr | head -10 |
      awk -F'|' '
      function human(b) {
        if (b >= 1073741824) return sprintf("%.1f GB", b/1073741824)
        if (b >= 1048576) return sprintf("%.1f MB", b/1048576)
        if (b >= 1024) return sprintf("%.1f KB", b/1024)
        return b " B"
      }
      NR == 1 { printf "  %-105s %10s %10s %8s\n", "Process", "Recv", "Sent", "Re-TX" }
      {
        name = $2; if (length(name) > 105) name = substr(name, 1, 102) "..."
        printf "  %-105s %10s %10s", name, human($3), human($4)
        if ($5 > 0) printf " %8d", $5; else printf " %8s", "-"
        printf "\n"
      }
    '

    # Total traffic
    awk -F, '
      function human(b) {
        if (b >= 1073741824) return sprintf("%.1f GB", b/1073741824)
        if (b >= 1048576) return sprintf("%.1f MB", b/1048576)
        if (b >= 1024) return sprintf("%.1f KB", b/1024)
        return b " B"
      }
      NR == 1 { next }
      { tin += $4; tout += $5 }
      END {
        printf "\n  Total recv: %s  |  Total sent: %s\n", human(tin), human(tout)
      }
    ' "$traffic_file"
    echo ""
  fi

  # --- Connections ---
  local conn_file="${target%.csv}-connections.csv"
  if [[ -f "$conn_file" ]] && [[ $(wc -l <"$conn_file") -gt 1 ]]; then
    _section "Top Connections (by remote host)"
    # Sum bytes per process+remote_ip, resolve IPs to hostnames, show top 15
    awk -F, '
      NR == 1 { next }
      {
        key = $2 "|" $4
        in_sum[key] += $6; out_sum[key] += $7; retx_sum[key] += $8
      }
      END {
        for (key in in_sum) {
          total = in_sum[key] + out_sum[key]
          if (total > 0)
            printf "%d|%s|%d|%d|%d\n", total, key, in_sum[key], out_sum[key], retx_sum[key]
        }
      }
    ' "$conn_file" | sort -t'|' -k1 -nr | head -15 | \
    while IFS='|' read -r _total proc remote_ip bytes_in bytes_out retx; do
      # Reverse DNS lookup
      hostname=$(host "$remote_ip" 2>/dev/null | awk '/domain name pointer/ {sub(/\.$/, "", $NF); print $NF; exit}')
      hostname="${hostname:-$remote_ip}"
      echo "${_total}|${proc}|${hostname}|${bytes_in}|${bytes_out}|${retx}"
    done | awk -F'|' '
      function human(b) {
        if (b >= 1073741824) return sprintf("%.1f GB", b/1073741824)
        if (b >= 1048576) return sprintf("%.1f MB", b/1048576)
        if (b >= 1024) return sprintf("%.1f KB", b/1024)
        return b " B"
      }
      NR == 1 { printf "  %-50s %-50s %10s %10s %8s\n", "Process", "Remote Host", "Recv", "Sent", "Re-TX" }
      {
        p = $2; if (length(p) > 50) p = substr(p, 1, 47) "..."
        h = $3; if (length(h) > 50) h = substr(h, 1, 47) "..."
        printf "  %-50s %-50s %10s %10s", p, h, human($4), human($5)
        if ($6 > 0) printf " %8d", $6; else printf " %8s", "-"
        printf "\n"
      }
    '
    echo ""
  fi

  # --- Problem detection ---
  _section "Issues Detected"
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

  # High retransmits (from traffic log)
  if [[ -f "$traffic_file" ]]; then
    local high_retx
    high_retx=$(awk -F, '
      NR == 1 { next }
      { retx[$2] += $8 }
      END {
        for (proc in retx)
          if (retx[proc] > 50) printf "       %s: %d retransmits\n", proc, retx[proc]
      }
    ' "$traffic_file")
    if [[ -n "$high_retx" ]]; then
      echo "  ⚠  High TCP retransmits (>50):"
      echo "$high_retx" | head -5
      issues=$((issues + 1))
    fi
  fi

  if [[ "$issues" -eq 0 ]]; then
    echo "  ✓  No significant issues detected."
  fi

  echo ""
  printf '═%.0s' $(seq 1 "$REPORT_WIDTH"); echo
  echo " Raw CSV     : $target"
  [[ -f "$traffic_file" ]] && echo " Traffic CSV : $traffic_file"
  [[ -f "$conn_file" ]] && echo " Connect CSV : $conn_file"
  printf '═%.0s' $(seq 1 "$REPORT_WIDTH"); echo
}

cmd_list() {
  echo "Available logs:"
  ls -lt "$LOG_DIR"/call-*.csv 2>/dev/null | grep -vE '(-traffic|-connections)' |
    awk '{print "  "$NF"  ("$5" bytes, "$6" "$7" "$8")"}' || echo "  (none)"
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
