#!/usr/bin/env bash
#
# netmon.sh
# Run in the background during calls to log network conditions.
# Usage:
#   ./netmon.sh start   # begin logging
#   ./netmon.sh stop    # stop logging
#   ./netmon.sh review  # pretty-print the latest log
#   ./netmon.sh list    # list available logs
#
# Logs are saved to ~/call-network-logs/

set -euo pipefail

LOG_DIR="${LOG_DIR:-$HOME/call-network-logs}"
PID_FILE="$LOG_DIR/.monitor.pid"

INTERVAL="${MONITOR_INTERVAL:-2}" # seconds between samples
PING_TARGET="${PING_TARGET:-8.8.8.8}"
PING_COUNT="${PING_COUNT:-3}"
PING_TIMEOUT_MS="${PING_TIMEOUT_MS:-2000}" # macOS ping uses milliseconds

REPORT_WIDTH=140

AIRPORT_PATH="/System/Library/PrivateFrameworks/Apple80211.framework/Versions/Current/Resources/airport"
WIFI_HELPER="$LOG_DIR/.wifi_helper"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
MONITOR_TUI_PY="$SCRIPT_DIR/netmon_tui.py"

MAIN_CSV_HEADER="timestamp,ssid,channel,rssi_dBm,noise_dBm,snr_dB,tx_rate_Mbps,interface,local_ip,public_ip,ping_target,loss_%,ping_min_ms,ping_avg_ms,ping_max_ms,dns_ms,gateway_ip,gw_ping_ms,jitter_ms,bssid,mcs,channel_band,channel_width,if_ierrs,if_oerrs,cpu_usage,mem_pressure"
TRAFFIC_CSV_HEADER="sample_ts,process,pid,bytes_in,bytes_out,rx_dupe,rx_ooo,retransmits"
CONNECTIONS_CSV_HEADER="sample_ts,process,pid,remote_ip,remote_port,bytes_in,bytes_out,retransmits"
SCAN_CSV_HEADER="scan_ts,ssid,bssid,rssi,channel,security"
SCAN_INTERVAL=15  # run wifi scan every N sample cycles

# -- generic helpers -------------------------------------------------

timestamp() { date "+%Y-%m-%d %H:%M:%S"; }

has_cmd() { command -v "$1" >/dev/null 2>&1; }

warn() { printf "Warning: %s\n" "$*" >&2; }

die() {
  printf "Error: %s\n" "$*" >&2
  exit 1
}

ensure_log_dir() { mkdir -p "$LOG_DIR"; }

assert_supported_os() {
  [[ "$(uname -s)" == "Darwin" ]] || die "netmon.sh currently supports macOS only."
}

validate_interval() {
  [[ "$INTERVAL" =~ ^[0-9]+([.][0-9]+)?$ ]] || die "MONITOR_INTERVAL must be a positive number."
}

repeat_char() {
  local char="$1" count="$2" i
  ((count > 0)) || return 0
  for ((i = 0; i < count; i++)); do
    printf "%s" "$char"
  done
}

print_rule() {
  repeat_char "=" "$REPORT_WIDTH"
  echo
}

_section() {
  local label="-- $1 "
  local pad=$((REPORT_WIDTH - ${#label}))
  printf "%s" "$label"
  repeat_char "-" "$pad"
  echo
}

sanitize_csv_field() {
  local value="${1:-}"
  value=${value//$'\n'/ }
  value=${value//$'\r'/ }
  value=${value//,/;}
  printf "%s" "$value"
}

make_tmp_file() {
  local suffix="$1" tmp
  tmp=$(mktemp -t "netmon.${suffix}.XXXXXX" 2>/dev/null) || tmp=""
  if [[ -z "$tmp" ]]; then
    tmp="/tmp/netmon_${suffix}_$$.$RANDOM"
    : >"$tmp"
  fi
  printf "%s\n" "$tmp"
}

latest_main_log() {
  local latest="" file
  shopt -s nullglob
  for file in "$LOG_DIR"/call-*.csv; do
    [[ "$file" == *-traffic.csv ]] && continue
    [[ "$file" == *-connections.csv ]] && continue
    [[ -z "$latest" || "$file" -nt "$latest" ]] && latest="$file"
  done
  shopt -u nullglob
  printf "%s\n" "$latest"
}

read_pid_file() {
  [[ -f "$PID_FILE" ]] || return 1
  local pid
  pid=$(cat "$PID_FILE" 2>/dev/null || true)
  [[ "$pid" =~ ^[0-9]+$ ]] || return 1
  printf "%s\n" "$pid"
}

pid_is_monitor() {
  local pid="$1"
  kill -0 "$pid" 2>/dev/null || return 1
  local cmdline
  cmdline=$(ps -p "$pid" -o command= 2>/dev/null || true)
  [[ "$cmdline" == *"netmon.sh"* ]]
}

# -- Wi-Fi helpers ---------------------------------------------------

maybe_compile_wifi_helper() {
  [[ -x "$WIFI_HELPER" ]] && return 0

  if [[ -x "$AIRPORT_PATH" ]] && "$AIRPORT_PATH" -I 2>/dev/null | grep -q "agrCtlRSSI"; then
    return 0
  fi

  if ! has_cmd swiftc; then
    warn "swiftc is not available; Wi-Fi RSSI/noise metrics may be unavailable."
    return 0
  fi

  local tmp_binary
  tmp_binary=$(make_tmp_file "wifi_helper")

  if swiftc -O -o "$tmp_binary" - 2>&1 | head -5 <<'SWIFT'
import CoreWLAN
guard let iface = CWWiFiClient.shared().interface() else { exit(1) }
print("     agrCtlRSSI: \(iface.rssiValue())")
print("     agrCtlNoise: \(iface.noiseMeasurement())")
let ssid = iface.ssid() ?? "unknown"
print("          SSID: \(ssid)")
let ch = iface.wlanChannel()
let chNum = ch?.channelNumber ?? 0
print("       channel: \(chNum)")
print("     lastTxRate: \(Int(iface.transmitRate()))")
let bssid = iface.bssid() ?? "?"
print("         BSSID: \(bssid)")
var chWidth = "?"
if let cw = ch {
    switch cw.channelWidth {
    case .width20MHz: chWidth = "20"
    case .width40MHz: chWidth = "40"
    case .width80MHz: chWidth = "80"
    case .width160MHz: chWidth = "160"
    @unknown default: chWidth = "?"
    }
}
print("  channelWidth: \(chWidth)")
SWIFT
  then
    mv "$tmp_binary" "$WIFI_HELPER"
    chmod +x "$WIFI_HELPER"
  else
    rm -f "$tmp_binary"
    warn "Failed to compile CoreWLAN helper; continuing without it."
  fi
}

get_wifi_info() {
  local out

  if [[ -x "$AIRPORT_PATH" ]]; then
    out=$("$AIRPORT_PATH" -I 2>/dev/null || true)
    if grep -q "agrCtlRSSI" <<<"$out"; then
      printf "%s\n" "$out"
      return 0
    fi
  fi

  if [[ -x "$WIFI_HELPER" ]]; then
    out=$("$WIFI_HELPER" 2>/dev/null || true)
    if grep -q "agrCtlRSSI" <<<"$out"; then
      printf "%s\n" "$out"
      return 0
    fi
  fi

  printf "%s\n" "wifi: unavailable"
}

parse_wifi_info() {
  awk '
    BEGIN {
      ssid = "unknown"; channel = "?"; rssi = "?"; noise = "?"
      tx_rate = "?"; bssid = "?"; mcs_idx = "?"; ch_width = "?"
    }
    $1 == "SSID:" {
      $1 = ""
      sub(/^ +/, "", $0)
      if ($0 != "") ssid = $0
    }
    $1 == "channel:" {
      raw = $2
      channel = raw
      sub(/,.*/, "", channel)
      if (raw ~ /,/) {
        cw = raw; sub(/[^,]*,/, "", cw)
        if (cw ~ /^[0-9]+$/) ch_width = cw
      }
    }
    $1 == "agrCtlRSSI:" { rssi = $2 }
    $1 == "agrCtlNoise:" { noise = $2 }
    $1 == "lastTxRate:" { tx_rate = $2 }
    $1 == "maxRate:" { tx_rate = $2 }
    $1 == "BSSID:" { bssid = $2 }
    $1 == "MCS:" { mcs_idx = $2 }
    $1 == "channelWidth:" { ch_width = $2 }
    END {
      printf "%s|%s|%s|%s|%s|%s|%s|%s\n", ssid, channel, rssi, noise, tx_rate, bssid, mcs_idx, ch_width
    }
  '
}

fallback_ssid() {
  has_cmd networksetup || return 0
  local line
  line=$(networksetup -getairportnetwork en0 2>/dev/null || true)
  case "$line" in
  *": "*)
    printf "%s\n" "${line#*: }"
    ;;
  *)
    return 0
    ;;
  esac
}

# -- active measurements --------------------------------------------

run_ping() {
  ping -c "$PING_COUNT" -W "$PING_TIMEOUT_MS" "$PING_TARGET" 2>/dev/null || true
}

parse_ping() {
  local output="$1"
  local loss="?" min="?" avg="?" max="?"

  loss=$(awk '
    /packet loss/ {
      for (i = 1; i <= NF; i++) {
        if ($i ~ /^[0-9.]+%$/) { sub(/%/, "", $i); print $i; exit }
      }
    }
  ' <<<"$output")
  loss="${loss:-?}"

  local stats
  stats=$(awk -F"[/ =]+" '
    /round-trip|rtt/ {
      for (i = 1; i <= NF; i++) {
        if ($i ~ /^[0-9]+([.][0-9]+)?$/) vals[++n] = $i
      }
    }
    END {
      if (n >= 3) printf "%s|%s|%s", vals[1], vals[2], vals[3]
    }
  ' <<<"$output")

  if [[ -n "$stats" ]]; then
    IFS="|" read -r min avg max <<<"$stats"
  fi

  printf "%s|%s|%s|%s\n" "${loss:-?}" "${min:-?}" "${avg:-?}" "${max:-?}"
}

get_dns_latency() {
  has_cmd nslookup || {
    echo "?"
    return 0
  }

  if has_cmd python3; then
    python3 - <<'PY' 2>/dev/null || echo "?"
import subprocess
import time

t0 = time.time()
try:
    subprocess.run(
        ["nslookup", "google.com"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        timeout=5,
        check=False,
    )
    print(int((time.time() - t0) * 1000))
except Exception:
    print("?")
PY
  else
    nslookup google.com >/dev/null 2>&1 || true
    echo "?"
  fi
}

get_active_interface() {
  route -n get default 2>/dev/null | awk '/interface:/ { print $2; found=1 } END { if (!found) print "unknown" }'
}

get_local_ip() {
  local iface="$1"
  ifconfig "$iface" 2>/dev/null | awk '/inet / { print $2; exit }'
}

get_public_ip() {
  if has_cmd curl; then
    curl -fsS --max-time 3 https://api.ipify.org 2>/dev/null || echo "?"
  else
    echo "?"
  fi
}

interface_counters() {
  # Read cumulative interface counters as: ibytes|obytes
  local iface="$1"
  [[ -n "$iface" ]] || return 1

  netstat -ibn -I "$iface" 2>/dev/null | awk -v iface="$iface" '
    NR == 1 {
      for (i = 1; i <= NF; i++) {
        if ($i == "Ibytes") in_col = i
        if ($i == "Obytes") out_col = i
      }
      next
    }
    $1 == iface && in_col > 0 && out_col > 0 {
      ib = $(in_col)
      ob = $(out_col)
      if (ib ~ /^[0-9]+$/ && ob ~ /^[0-9]+$/) {
        if (!seen || ib + 0 > max_in) max_in = ib + 0
        if (!seen || ob + 0 > max_out) max_out = ob + 0
        seen = 1
      }
    }
    END {
      if (seen) printf "%d|%d\n", max_in, max_out
    }
  '
}

# -- extended metrics ------------------------------------------------

get_gateway_ip() {
  route -n get default 2>/dev/null | awk '/gateway:/ { print $2; exit }'
}

get_gateway_ping() {
  local gw="$1"
  [[ -n "$gw" && "$gw" != "?" ]] || { echo "?"; return 0; }
  local ms
  ms=$(ping -c 1 -W 500 "$gw" 2>/dev/null | awk '/time=/ { for(i=1;i<=NF;i++) if($i ~ /^time=/) { sub(/time=/, "", $i); printf "%.1f", $i+0; exit } }' || true)
  echo "${ms:-?}"
}

parse_jitter() {
  local output="$1"
  printf "%s\n" "$output" | awk '
    /time=/ {
      for (i = 1; i <= NF; i++) {
        if ($i ~ /^time=/) {
          sub(/time=/, "", $i)
          t[++n] = $i + 0
        }
      }
    }
    END {
      if (n < 2) { print "?"; exit }
      sum = 0
      for (i = 1; i <= n; i++) sum += t[i]
      mean = sum / n
      jsum = 0
      for (i = 1; i <= n; i++) {
        d = t[i] - mean
        if (d < 0) d = -d
        jsum += d
      }
      printf "%.1f", jsum / n
    }
  '
}

channel_to_band() {
  local ch="$1"
  [[ "$ch" =~ ^[0-9]+$ ]] || { echo "?"; return 0; }
  if (( ch >= 1 && ch <= 14 )); then
    echo "2.4"
  elif (( ch >= 32 && ch <= 177 )); then
    echo "5"
  else
    echo "?"
  fi
}

get_cpu_usage() {
  ps -A -o %cpu= 2>/dev/null | awk '{s+=$1}END{printf "%.0f", s+0}'
}

get_mem_pressure() {
  vm_stat 2>/dev/null | awk '
    function num(s) { gsub(/[^0-9]/, "", s); return s+0 }
    /Pages free:/ { free = num($NF) }
    /Pages active:/ { active = num($NF) }
    /Pages inactive:/ { inactive = num($NF) }
    /Pages speculative:/ { spec = num($NF) }
    /Pages wired down:/ { wired = num($NF) }
    /compressor:/ { comp = num($NF) }
    END {
      used = active + wired + comp
      total = free + active + inactive + spec + wired + comp
      if (total > 0) printf "%.0f", (used * 100.0 / total)
      else print "?"
    }
  '
}

get_interface_errors() {
  local iface="$1"
  [[ -n "$iface" ]] || { echo "0|0"; return 0; }
  netstat -ibn -I "$iface" 2>/dev/null | awk -v iface="$iface" '
    NR == 1 {
      for (i = 1; i <= NF; i++) {
        if ($i == "Ierrs") ie_col = i
        if ($i == "Oerrs") oe_col = i
      }
      next
    }
    $1 == iface && ie_col > 0 && oe_col > 0 {
      ie = $(ie_col)
      oe = $(oe_col)
      if (ie ~ /^[0-9]+$/ && oe ~ /^[0-9]+$/) {
        if (!seen || ie+0 > max_ie) max_ie = ie+0
        if (!seen || oe+0 > max_oe) max_oe = oe+0
        seen = 1
      }
    }
    END {
      if (seen) printf "%d|%d\n", max_ie, max_oe
      else print "0|0"
    }
  '
}

run_wifi_scan() {
  # Uses system_profiler to get nearby networks + extended current network info.
  # Also writes PHY mode and MCS to a sidecar file for the sample loop.
  local scan_file="$1" ts="$2" ext_file="$3"
  local sp_output
  sp_output=$(system_profiler SPAirPortDataType 2>/dev/null || true)
  [[ -n "$sp_output" ]] || return 0

  # Extract current network PHY mode and MCS into sidecar file
  printf "%s\n" "$sp_output" | awk '
    /Current Network Information:/,/Other Local Wi-Fi Networks:/ {
      if (/PHY Mode:/) { sub(/.*PHY Mode: */, ""); print "phy_mode=" $0 }
      if (/MCS Index:/) { sub(/.*MCS Index: */, ""); print "mcs_index=" $0 }
    }
  ' >"$ext_file"

  # Extract neighboring networks into scan CSV
  printf "%s\n" "$sp_output" | awk -v ts="$ts" '
    /Other Local Wi-Fi Networks:/,0 {
      if (/Other Local Wi-Fi Networks:/) { next }
      if (/:$/) {
        if (ssid != "" && ch != "") {
          gsub(/,/, ";", ssid)
          gsub(/,/, ";", ch)
          gsub(/,/, ";", sec)
          printf "%s,%s,%s,%s,%s,%s\n", ts, ssid, "-", rssi, ch, sec
        }
        ssid = $0; gsub(/^ *| *:$/, "", ssid)
        ch = ""; rssi = "?"; sec = ""
        next
      }
      if (/Channel:/) {
        sub(/.*Channel: */, "")
        ch = $0
      }
      if (/Security:/) { sub(/.*Security: */, ""); sec = $0 }
      if (/Signal \/ Noise:/) {
        sub(/.*Signal \/ Noise: */, "")
        sub(/ dBm.*/, "")
        rssi = $0
      }
    }
    END {
      if (ssid != "" && ch != "") {
        gsub(/,/, ";", ssid)
        gsub(/,/, ";", ch)
        gsub(/,/, ";", sec)
        printf "%s,%s,%s,%s,%s,%s\n", ts, ssid, "-", rssi, ch, sec
      }
    }
  ' >>"$scan_file"
}

# -- nettop snapshots ------------------------------------------------

_nettop_snapshot() {
  # Raw nettop snapshot: process.pid,bytes_in,bytes_out,rx_dupe,rx_ooo,re-tx
  nettop -P -L 1 -n -x -J time,bytes_in,bytes_out,rx_dupe,rx_ooo,re-tx 2>/dev/null |
    awk -F, 'NR > 1 && ($3 + 0 > 0 || $4 + 0 > 0) { print $2 "," $3 "," $4 "," $5 "," $6 "," $7 }'
}

_nettop_conn_snapshot() {
  # Connection-level snapshot:
  #   process.pid|local_ip:port<->remote_ip:port,bytes_in,bytes_out,retransmits
  # Keep full flow key so multiple sockets to the same remote do not collide.
  nettop -m tcp -L 1 -n -x 2>/dev/null | awk -F, '
    NR == 1 { next }
    # Process summary line (interface field is empty)
    $3 == "" && $2 ~ /\.[0-9]+$/ { proc = $2; next }
    # Connection line with traffic
    $2 ~ /<->/ && ($5 + 0 > 0 || $6 + 0 > 0) {
      flow = $2
      gsub(/^[[:space:]]+|[[:space:]]+$/, "", flow)
      print proc "|" flow "," $5 "," $6 "," $9
    }
  '
}

build_name_map() {
  local curr_file="$1" name_file="$2"
  local pids

  pids=$(awk -F, '
    {
      n = split($1, p, ".")
      if (n > 1 && p[n] ~ /^[0-9]+$/) print p[n]
    }
  ' "$curr_file" | sort -u | paste -sd, -)

  if [[ -z "$pids" ]]; then
    : >"$name_file"
    return 0
  fi

  if ! ps -p "$pids" -o pid=,comm= 2>/dev/null | \
    awk '{
      pid = $1
      $1 = ""
      sub(/^ +/, "")
      n = split($0, a, "/")
      print pid "," a[n]
    }' >"$name_file"; then
    : >"$name_file"
  fi
}

capture_traffic() {
  # Compute per-interval deltas against previous snapshot, append to traffic CSV
  local ts="$1" traffic_file="$2" prev_file="$3" curr_file="$4" name_file="$5"

  if [[ -s "$prev_file" ]]; then
    awk -F, -v ts="$ts" '
      FILENAME == ARGV[1] { fullname[$1] = $2; next }
      FILENAME == ARGV[2] { prev_in[$1]=$2; prev_out[$1]=$3; prev_dup[$1]=$4; prev_ooo[$1]=$5; prev_retx[$1]=$6; next }
      {
        din   = $2 - (prev_in[$1] + 0);    if (din < 0) din = 0
        dout  = $3 - (prev_out[$1] + 0);   if (dout < 0) dout = 0
        ddup  = $4 - (prev_dup[$1] + 0);   if (ddup < 0) ddup = 0
        dooo  = $5 - (prev_ooo[$1] + 0);   if (dooo < 0) dooo = 0
        dretx = $6 - (prev_retx[$1] + 0);  if (dretx < 0) dretx = 0
        if (din > 0 || dout > 0) {
          proc = $1
          pid = ""
          n = split(proc, p, ".")
          if (n > 1 && p[n] ~ /^[0-9]+$/) {
            pid = p[n]
            proc = p[1]
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

capture_connections() {
  local ts="$1" conn_file="$2" prev_file="$3" curr_file="$4" name_file="$5"

  _nettop_conn_snapshot >"$curr_file"

  if [[ -s "$prev_file" ]]; then
    awk -F, -v ts="$ts" '
      FILENAME == ARGV[1] { fullname[$1] = $2; next }
      FILENAME == ARGV[2] { prev_in[$1]=$2; prev_out[$1]=$3; prev_retx[$1]=$4; next }
      {
        din   = $2 - (prev_in[$1] + 0);    if (din < 0) din = 0
        dout  = $3 - (prev_out[$1] + 0);   if (dout < 0) dout = 0
        dretx = $4 - (prev_retx[$1] + 0);  if (dretx < 0) dretx = 0
        if (din > 0 || dout > 0) {
          split($1, kp, "|")
          proc_raw = kp[1]
          flow = kp[2]
          split(flow, lr, "<->")
          remote = (length(lr[2]) > 0 ? lr[2] : flow)
          gsub(/^[[:space:]]+|[[:space:]]+$/, "", remote)
          pid = ""
          n = split(proc_raw, p, ".")
          if (n > 1 && p[n] ~ /^[0-9]+$/) {
            pid = p[n]
            proc = p[1]
            for (i = 2; i < n; i++) proc = proc "." p[i]
          } else {
            proc = proc_raw
          }
          if (pid != "" && pid in fullname) proc = fullname[pid]
          n = split(remote, rp, ":")
          rport = rp[n]
          rip = rp[1]
          for (i = 2; i < n; i++) rip = rip ":" rp[i]
          printf "%s,%s,%s,%s,%s,%d,%d,%d\n", ts, proc, pid, rip, rport, din, dout, dretx
        }
      }
    ' "$name_file" "$prev_file" "$curr_file" >>"$conn_file"
  fi

  cp "$curr_file" "$prev_file"
}

# -- sampling loop ---------------------------------------------------

sample_loop() {
  # Disable errexit: the monitor must survive transient failures
  # (e.g. WiFi drops during network switch)
  set +e

  local logfile="$1" traffic_file="$2" conn_file="$3" scan_file="$4"

  echo "$MAIN_CSV_HEADER" >"$logfile"
  echo "$TRAFFIC_CSV_HEADER" >"$traffic_file"
  echo "$CONNECTIONS_CSV_HEADER" >"$conn_file"
  echo "$SCAN_CSV_HEADER" >"$scan_file"

  local pub_ip
  pub_ip=$(get_public_ip)
  pub_ip="${pub_ip:-?}"

  local ping_file dns_file gw_ping_file prev_traffic curr_traffic prev_conn curr_conn name_file ext_file
  ping_file=$(make_tmp_file "ping")
  dns_file=$(make_tmp_file "dns")
  gw_ping_file=$(make_tmp_file "gwping")
  prev_traffic=$(make_tmp_file "tprev")
  curr_traffic=$(make_tmp_file "tcurr")
  prev_conn=$(make_tmp_file "cprev")
  curr_conn=$(make_tmp_file "ccurr")
  name_file=$(make_tmp_file "names")
  ext_file=$(make_tmp_file "ext")

  # shellcheck disable=SC2064
  trap "rm -f '$ping_file' '$dns_file' '$gw_ping_file' '$prev_traffic' '$curr_traffic' '$prev_conn' '$curr_conn' '$name_file' '$ext_file'" EXIT INT TERM

  # Baseline snapshots (not logged; used as zero point)
  _nettop_snapshot >"$prev_traffic" || : >"$prev_traffic"
  _nettop_conn_snapshot >"$prev_conn" || : >"$prev_conn"

  # Baseline interface errors
  local prev_ierrs=0 prev_oerrs=0
  local scan_counter=0

  while true; do
    local ts wifi_info parsed_wifi ssid channel rssi noise tx_rate
    local bssid mcs_idx raw_channel_width
    local iface lip snr ping_pid dns_pid gw_ping_pid ping_output ping_parsed
    local loss pmin pavg pmax dns_ms
    local gateway_ip gw_ping_ms jitter_ms channel_band
    local curr_ierrs curr_oerrs if_ierrs if_oerrs
    local cpu_usage mem_pressure

    ts=$(timestamp)

    wifi_info=$(get_wifi_info || true)
    parsed_wifi=$(printf "%s\n" "$wifi_info" | parse_wifi_info)
    IFS="|" read -r ssid channel rssi noise tx_rate bssid mcs_idx raw_channel_width <<<"$parsed_wifi"

    if [[ -z "$ssid" || "$ssid" == "unknown" ]]; then
      ssid=$(fallback_ssid || true)
      ssid="${ssid:-unknown}"
    fi

    iface=$(get_active_interface || echo "unknown")
    iface="${iface:-unknown}"
    lip=$(get_local_ip "$iface" 2>/dev/null || true)
    lip="${lip:-?}"

    if [[ "$rssi" =~ ^-?[0-9]+$ && "$noise" =~ ^-?[0-9]+$ && "$noise" != "0" ]]; then
      snr=$((rssi - noise))
    else
      snr="?"
    fi

    # Derive band from channel number
    channel_band=$(channel_to_band "$channel")

    # Get gateway
    gateway_ip=$(get_gateway_ip || true)
    gateway_ip="${gateway_ip:-?}"

    # Spawn ping, DNS, and gateway ping in background
    run_ping >"$ping_file" &
    ping_pid=$!
    get_dns_latency >"$dns_file" &
    dns_pid=$!
    get_gateway_ping "$gateway_ip" >"$gw_ping_file" &
    gw_ping_pid=$!

    # Traffic snapshots (while pings are in flight)
    _nettop_snapshot >"$curr_traffic" 2>/dev/null || : >"$curr_traffic"
    build_name_map "$curr_traffic" "$name_file" 2>/dev/null || true
    capture_traffic "$ts" "$traffic_file" "$prev_traffic" "$curr_traffic" "$name_file" || true
    capture_connections "$ts" "$conn_file" "$prev_conn" "$curr_conn" "$name_file" || true

    # System metrics (fast, no background needed)
    cpu_usage=$(get_cpu_usage)
    cpu_usage="${cpu_usage:-?}"
    mem_pressure=$(get_mem_pressure)
    mem_pressure="${mem_pressure:-?}"

    # Interface errors (delta since last sample)
    local err_data
    err_data=$(get_interface_errors "$iface" || echo "0|0")
    IFS="|" read -r curr_ierrs curr_oerrs <<<"$err_data"
    curr_ierrs="${curr_ierrs:-0}"
    curr_oerrs="${curr_oerrs:-0}"
    if_ierrs=$(( (curr_ierrs + 0) - (prev_ierrs + 0) ))
    if_oerrs=$(( (curr_oerrs + 0) - (prev_oerrs + 0) ))
    [[ "$if_ierrs" -lt 0 ]] 2>/dev/null && if_ierrs=0
    [[ "$if_oerrs" -lt 0 ]] 2>/dev/null && if_oerrs=0
    prev_ierrs=$curr_ierrs
    prev_oerrs=$curr_oerrs

    # Wait for background network probes
    wait "$ping_pid" 2>/dev/null || true
    wait "$dns_pid" 2>/dev/null || true
    wait "$gw_ping_pid" 2>/dev/null || true

    ping_output=$(cat "$ping_file" 2>/dev/null || true)
    ping_parsed=$(parse_ping "$ping_output")
    IFS="|" read -r loss pmin pavg pmax <<<"$ping_parsed"

    dns_ms=$(cat "$dns_file" 2>/dev/null || true)
    dns_ms="${dns_ms:-?}"

    gw_ping_ms=$(cat "$gw_ping_file" 2>/dev/null || true)
    gw_ping_ms="${gw_ping_ms:-?}"

    jitter_ms=$(parse_jitter "$ping_output")
    jitter_ms="${jitter_ms:-?}"

    # Read extended WiFi info from system_profiler sidecar (if available)
    if [[ -s "$ext_file" ]]; then
      local sp_phy sp_mcs
      sp_phy=$(awk -F= '/^phy_mode=/ {print $2}' "$ext_file")
      sp_mcs=$(awk -F= '/^mcs_index=/ {print $2}' "$ext_file")
      [[ -n "$sp_phy" ]] && mcs_idx="${sp_mcs:-$mcs_idx}"
    fi

    # WiFi scan via system_profiler (every SCAN_INTERVAL cycles, in background)
    scan_counter=$((scan_counter + 1))
    if (( scan_counter >= SCAN_INTERVAL )); then
      scan_counter=0
      run_wifi_scan "$scan_file" "$ts" "$ext_file" &
    fi

    printf "%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s\n" \
      "$(sanitize_csv_field "$ts")" \
      "$(sanitize_csv_field "$ssid")" \
      "$(sanitize_csv_field "$channel")" \
      "$(sanitize_csv_field "$rssi")" \
      "$(sanitize_csv_field "$noise")" \
      "$(sanitize_csv_field "$snr")" \
      "$(sanitize_csv_field "$tx_rate")" \
      "$(sanitize_csv_field "$iface")" \
      "$(sanitize_csv_field "$lip")" \
      "$(sanitize_csv_field "$pub_ip")" \
      "$(sanitize_csv_field "$PING_TARGET")" \
      "$(sanitize_csv_field "$loss")" \
      "$(sanitize_csv_field "$pmin")" \
      "$(sanitize_csv_field "$pavg")" \
      "$(sanitize_csv_field "$pmax")" \
      "$(sanitize_csv_field "$dns_ms")" \
      "$(sanitize_csv_field "$gateway_ip")" \
      "$(sanitize_csv_field "$gw_ping_ms")" \
      "$(sanitize_csv_field "$jitter_ms")" \
      "$(sanitize_csv_field "${bssid:-?}")" \
      "$(sanitize_csv_field "${mcs_idx:-?}")" \
      "$(sanitize_csv_field "$channel_band")" \
      "$(sanitize_csv_field "${raw_channel_width:-?}")" \
      "$(sanitize_csv_field "$if_ierrs")" \
      "$(sanitize_csv_field "$if_oerrs")" \
      "$(sanitize_csv_field "$cpu_usage")" \
      "$(sanitize_csv_field "$mem_pressure")" >>"$logfile"

    sleep "$INTERVAL"
  done
}

# -- commands --------------------------------------------------------

cmd_start() {
  assert_supported_os
  ensure_log_dir
  validate_interval
  maybe_compile_wifi_helper

  local existing_pid
  if existing_pid=$(read_pid_file 2>/dev/null); then
    if pid_is_monitor "$existing_pid"; then
      echo "Monitor already running (PID $existing_pid)."
      local running_log
      running_log=$(latest_main_log)
      [[ -n "$running_log" ]] && echo "Log: $running_log"
      return 0
    fi
    warn "Removing stale PID file."
    rm -f "$PID_FILE"
  fi

  local stamp logfile traffic_file conn_file scan_file
  stamp=$(date +%Y%m%d-%H%M%S)
  logfile="$LOG_DIR/call-${stamp}.csv"
  traffic_file="$LOG_DIR/call-${stamp}-traffic.csv"
  conn_file="$LOG_DIR/call-${stamp}-connections.csv"
  scan_file="$LOG_DIR/call-${stamp}-scan.csv"

  echo "Starting network monitor..."
  echo "  Log file : $logfile"
  echo "  Traffic  : $traffic_file"
  echo "  Connects : $conn_file"
  echo "  WiFi scan: $scan_file"
  echo "  Interval : ${INTERVAL}s"
  echo "  Ping host: $PING_TARGET"

  sample_loop "$logfile" "$traffic_file" "$conn_file" "$scan_file" &
  local pid=$!
  echo "$pid" >"$PID_FILE"
  echo "  PID      : $pid"
  echo
  echo "Run './netmon.sh stop' when your call ends."
}

cmd_stop() {
  local pid
  if ! pid=$(read_pid_file 2>/dev/null); then
    echo "No monitor running."
    return 0
  fi

  if ! kill -0 "$pid" 2>/dev/null; then
    echo "Monitor process $pid is already gone."
  elif ! pid_is_monitor "$pid"; then
    warn "PID $pid is not a netmon process. Not sending kill."
  else
    kill "$pid" 2>/dev/null || true
    local i
    for ((i = 0; i < 20; i++)); do
      if ! kill -0 "$pid" 2>/dev/null; then
        break
      fi
      sleep 0.1
    done
    if kill -0 "$pid" 2>/dev/null; then
      warn "Process did not stop after SIGTERM, sending SIGKILL."
      kill -9 "$pid" 2>/dev/null || true
    fi
    echo "Stopped monitor (PID $pid)."
  fi

  rm -f "$PID_FILE"

  local latest
  latest=$(latest_main_log)
  if [[ -n "$latest" ]]; then
    local samples traffic conns
    samples=$(($(wc -l <"$latest") - 1))
    echo "Logged $samples samples to: $latest"
    traffic="${latest%.csv}-traffic.csv"
    [[ -f "$traffic" ]] && echo "Traffic log    : $traffic"
    conns="${latest%.csv}-connections.csv"
    [[ -f "$conns" ]] && echo "Connections log: $conns"
  fi
}

running_monitor_pid() {
  local pid
  pid=$(read_pid_file 2>/dev/null || true)
  [[ -n "$pid" ]] || return 1
  pid_is_monitor "$pid" || return 1
  printf "%s\n" "$pid"
}

cmd_monitor() {
  ensure_log_dir
  assert_supported_os

  local attach_only=0 keep_running=0
  while [[ $# -gt 0 ]]; do
    case "$1" in
    --attach)
      attach_only=1
      ;;
    --keep-running)
      keep_running=1
      ;;
    -h | --help)
      echo "Usage: $0 monitor [--attach] [--keep-running]"
      echo "  --attach       Do not start collector; attach to latest running session"
      echo "  --keep-running Keep collector running after quitting TUI"
      return 0
      ;;
    *)
      die "Unknown monitor option: $1"
      ;;
    esac
    shift
  done

  has_cmd python3 || die "python3 is required for monitor TUI."
  [[ -f "$MONITOR_TUI_PY" ]] || die "Monitor UI script not found: $MONITOR_TUI_PY"

  local started_here=0
  if ! running_monitor_pid >/dev/null 2>&1; then
    if [[ "$attach_only" -eq 1 ]]; then
      die "No running collector to attach to."
    fi
    cmd_start
    started_here=1
    sleep 0.2
  fi

  local main_file
  main_file=$(latest_main_log)
  [[ -n "$main_file" ]] || die "No main log file found to monitor."

  local rc=0
  python3 "$MONITOR_TUI_PY" \
    --main-file "$main_file" \
    --log-dir "$LOG_DIR" \
    --pid-file "$PID_FILE" \
    --refresh 1.0 || rc=$?

  if [[ "$started_here" -eq 1 && "$keep_running" -eq 0 ]]; then
    cmd_stop || true
  fi

  return "$rc"
}

cmd_measure() {
  assert_supported_os
  has_cmd netstat || die "netstat is required for interface measurement."

  local duration="60" iface="" opt=""
  while [[ $# -gt 0 ]]; do
    case "$1" in
    -d | --duration)
      opt="$1"
      shift
      [[ $# -gt 0 ]] || die "Missing value for $opt"
      duration="$1"
      ;;
    -i | --interface)
      opt="$1"
      shift
      [[ $# -gt 0 ]] || die "Missing value for $opt"
      iface="$1"
      ;;
    -h | --help)
      echo "Usage: $0 measure [--duration seconds] [--interface en0]"
      echo "  --duration  Sample window in seconds (default: 60)"
      echo "  --interface Interface name (default: current route interface)"
      return 0
      ;;
    *)
      die "Unknown measure option: $1"
      ;;
    esac
    shift
  done

  [[ "$duration" =~ ^[0-9]+([.][0-9]+)?$ ]] || die "Duration must be numeric."

  if [[ -z "$iface" ]]; then
    iface=$(get_active_interface)
  fi
  [[ -n "$iface" && "$iface" != "unknown" ]] || die "Could not detect interface; pass --interface."

  local start_ts end_ts start_in start_out end_in end_out
  IFS="|" read -r start_in start_out <<<"$(interface_counters "$iface")"
  [[ -n "${start_in:-}" && -n "${start_out:-}" ]] || die "Failed to read counters for interface '$iface'."

  start_ts=$(timestamp)
  sleep "$duration"
  end_ts=$(timestamp)

  IFS="|" read -r end_in end_out <<<"$(interface_counters "$iface")"
  [[ -n "${end_in:-}" && -n "${end_out:-}" ]] || die "Failed to read counters for interface '$iface' after sampling."

  local recv_bytes sent_bytes total_bytes
  recv_bytes=$((end_in - start_in))
  sent_bytes=$((end_out - start_out))
  ((recv_bytes < 0)) && recv_bytes=0
  ((sent_bytes < 0)) && sent_bytes=0
  total_bytes=$((recv_bytes + sent_bytes))

  awk -v iface="$iface" \
    -v start_ts="$start_ts" \
    -v end_ts="$end_ts" \
    -v sec="$duration" \
    -v recv="$recv_bytes" \
    -v sent="$sent_bytes" \
    -v total="$total_bytes" '
    function human(b) {
      if (b >= 1073741824) return sprintf("%.2f GB", b / 1073741824)
      if (b >= 1048576) return sprintf("%.2f MB", b / 1048576)
      if (b >= 1024) return sprintf("%.2f KB", b / 1024)
      return sprintf("%d B", b)
    }
    BEGIN {
      if (sec <= 0) sec = 1
      print "===================================================================="
      print " Interface Throughput Sample"
      print "===================================================================="
      printf " Interface : %s\n", iface
      printf " Window    : %s -> %s (%.1fs)\n", start_ts, end_ts, sec + 0
      print ""
      printf " Recv      : %s\n", human(recv + 0)
      printf " Sent      : %s\n", human(sent + 0)
      printf " Total     : %s\n", human(total + 0)
      print ""
      printf " Avg recv/s: %s/s\n", human((recv + 0) / sec)
      printf " Avg sent/s: %s/s\n", human((sent + 0) / sec)
      printf " Avg total : %s/s\n", human((total + 0) / sec)
      print "===================================================================="
    }
  '
}

resolve_related_logs() {
  local input="$1"
  local main_file traffic_file conn_file kind

  case "$input" in
  *-traffic.csv)
    main_file="${input%-traffic.csv}.csv"
    traffic_file="$input"
    conn_file="${input%-traffic.csv}-connections.csv"
    kind="traffic"
    ;;
  *-connections.csv)
    main_file="${input%-connections.csv}.csv"
    traffic_file="${input%-connections.csv}-traffic.csv"
    conn_file="$input"
    kind="connections"
    ;;
  *)
    main_file="$input"
    traffic_file="${input%.csv}-traffic.csv"
    conn_file="${input%.csv}-connections.csv"
    kind="main"
    ;;
  esac

  printf "%s|%s|%s|%s\n" "$main_file" "$traffic_file" "$conn_file" "$kind"
}

is_main_log_file() {
  local file="$1"
  [[ -f "$file" ]] || return 1
  [[ "$(head -n1 "$file" 2>/dev/null || true)" == "$MAIN_CSV_HEADER" ]]
}

cmd_review() {
  ensure_log_dir
  local requested="${1:-}"

  if [[ -z "$requested" ]]; then
    requested=$(latest_main_log)
  fi

  if [[ -z "$requested" || ! -f "$requested" ]]; then
    echo "No log files found in $LOG_DIR"
    return 1
  fi

  local main_file traffic_file conn_file requested_kind
  IFS="|" read -r main_file traffic_file conn_file requested_kind <<<"$(resolve_related_logs "$requested")"

  local main_available=0
  if is_main_log_file "$main_file"; then
    main_available=1
  elif [[ "$requested_kind" == "main" ]]; then
    echo "Provided file is not a netmon main CSV: $requested"
    echo "Use a main file (call-*.csv) or any related -traffic/-connections file."
    return 1
  fi

  local sample_file="$requested"
  if [[ "$main_available" -eq 1 ]]; then
    sample_file="$main_file"
  fi

  local samples first last
  samples=$(($(wc -l <"$sample_file") - 1))
  if [[ "$samples" -gt 0 ]]; then
    first=$(sed -n "2p" "$sample_file" | cut -d, -f1)
    last=$(tail -1 "$sample_file" | cut -d, -f1)
  else
    first="n/a"
    last="n/a"
  fi

  local report_ping_target="$PING_TARGET"
  if [[ "$main_available" -eq 1 ]]; then
    local parsed_ping_target
    parsed_ping_target=$(awk -F, 'NR == 2 && $11 != "" { print $11; exit }' "$main_file")
    report_ping_target="${parsed_ping_target:-$PING_TARGET}"
  fi

  print_rule
  echo " Call Network Report"
  print_rule
  echo " File     : $sample_file"
  [[ "$requested" != "$sample_file" ]] && echo " Input    : $requested"
  echo " Samples  : $samples"
  echo " Period   : $first  ->  $last"
  echo

  if [[ "$main_available" -eq 1 ]]; then
    # Ping stats
    _section "Ping ($report_ping_target)"
    awk -F, '
      NR > 1 && $14 ~ /^[0-9.]+$/ {
        sum += $14; n++
        if (n == 1 || $14 + 0 < mn) mn = $14 + 0
        if (n == 1 || $14 + 0 > mx) mx = $14 + 0
      }
      END {
        if (n > 0) {
          printf "  Avg latency : %.1f ms\n", sum / n
          printf "  Min         : %.1f ms\n", mn
          printf "  Max         : %.1f ms\n", mx
        } else {
          print "  No valid ping data"
        }
      }
    ' "$main_file"

    awk -F, '
      NR > 1 && $12 ~ /^[0-9.]+$/ {
        sum += $12; n++
        if ($12 + 0 > 0) bad++
      }
      END {
        if (n > 0) {
          printf "  Avg loss    : %.1f%%\n", sum / n
          printf "  Samples w/ loss: %d/%d\n", bad + 0, n
        }
      }
    ' "$main_file"
    echo

    # Wi-Fi signal
    _section "Wi-Fi Signal"
    awk -F, '
      NR > 1 && $4 ~ /^-?[0-9]+$/ {
        sum += $4; n++
        if (n == 1 || $4 + 0 < mn) mn = $4 + 0
        if (n == 1 || $4 + 0 > mx) mx = $4 + 0
      }
      END {
        if (n > 0) {
          avg = sum / n
          printf "  RSSI avg    : %.0f dBm", avg
          if (avg > -50) printf "  (excellent)\n"
          else if (avg > -60) printf "  (good)\n"
          else if (avg > -70) printf "  (fair)\n"
          else printf "  (weak)\n"
          printf "  RSSI range  : %d to %d dBm\n", mn, mx
        } else {
          print "  No RSSI data"
        }
      }
    ' "$main_file"

    awk -F, '
      NR > 1 && $6 ~ /^-?[0-9]+$/ {
        sum += $6; n++
      }
      END {
        if (n > 0) {
          avg = sum / n
          printf "  SNR avg     : %.0f dB", avg
          if (avg > 40) printf "  (excellent)\n"
          else if (avg > 25) printf "  (good)\n"
          else if (avg > 15) printf "  (fair)\n"
          else printf "  (poor)\n"
        }
      }
    ' "$main_file"

    awk -F, '
      NR > 1 && $7 ~ /^[0-9]+$/ {
        sum += $7; n++
        if (n == 1 || $7 + 0 < mn) mn = $7 + 0
      }
      END {
        if (n > 0) printf "  TX rate avg : %.0f Mbps (min: %.0f)\n", sum / n, mn
      }
    ' "$main_file"
    echo

    # DNS
    _section "DNS Latency"
    awk -F, '
      NR > 1 && $16 ~ /^[0-9]+$/ {
        sum += $16; n++
        if (n == 1 || $16 + 0 > mx) mx = $16 + 0
      }
      END {
        if (n > 0) {
          printf "  Avg         : %.0f ms\n", sum / n
          printf "  Max         : %.0f ms\n", mx
        } else {
          print "  No DNS data"
        }
      }
    ' "$main_file"
    echo
  else
    _section "Main Metrics"
    echo "  Main sample CSV not found: $main_file"
    echo "  Ping/Wi-Fi/DNS sections are unavailable for this input alone."
    echo
  fi

  # Traffic
  if [[ -f "$traffic_file" ]]; then
    _section "Per-Process Traffic"
    awk -F, '
      NR == 1 { next }
      {
        in_sum[$2] += $4
        out_sum[$2] += $5
        retx_sum[$2] += $8
      }
      END {
        for (proc in in_sum) {
          total = in_sum[proc] + out_sum[proc]
          if (total > 0) printf "%d|%s|%d|%d|%d\n", total, proc, in_sum[proc], out_sum[proc], retx_sum[proc]
        }
      }
    ' "$traffic_file" | sort -t"|" -k1 -nr | head -10 |
      awk -F"|" '
        function human(b) {
          if (b >= 1073741824) return sprintf("%.1f GB", b / 1073741824)
          if (b >= 1048576) return sprintf("%.1f MB", b / 1048576)
          if (b >= 1024) return sprintf("%.1f KB", b / 1024)
          return b " B"
        }
        NR == 1 { printf "  %-105s %10s %10s %8s\n", "Process", "Recv", "Sent", "Re-TX" }
        {
          name = $2
          if (length(name) > 105) name = substr(name, 1, 102) "..."
          printf "  %-105s %10s %10s", name, human($3), human($4)
          if ($5 > 0) printf " %8d", $5; else printf " %8s", "-"
          printf "\n"
        }
      '

    awk -F, '
      function human(b) {
        if (b >= 1073741824) return sprintf("%.1f GB", b / 1073741824)
        if (b >= 1048576) return sprintf("%.1f MB", b / 1048576)
        if (b >= 1024) return sprintf("%.1f KB", b / 1024)
        return b " B"
      }
      NR == 1 { next }
      { tin += $4; tout += $5 }
      END {
        printf "\n  Total recv: %s  |  Total sent: %s\n", human(tin + 0), human(tout + 0)
      }
    ' "$traffic_file"
    echo
  fi

  # Connections
  if [[ -f "$conn_file" ]] && [[ $(wc -l <"$conn_file") -gt 1 ]]; then
    _section "Top Connections (by remote host)"
    awk -F, '
      NR == 1 { next }
      {
        key = $2 "|" $4
        in_sum[key] += $6
        out_sum[key] += $7
        retx_sum[key] += $8
      }
      END {
        for (key in in_sum) {
          total = in_sum[key] + out_sum[key]
          if (total > 0) printf "%d|%s|%d|%d|%d\n", total, key, in_sum[key], out_sum[key], retx_sum[key]
        }
      }
    ' "$conn_file" | sort -t"|" -k1 -nr | head -15 |
      while IFS="|" read -r total proc remote_ip bytes_in bytes_out retx; do
        local hostname
        if has_cmd host; then
          hostname=$(host "$remote_ip" 2>/dev/null | awk '/domain name pointer/ { sub(/\.$/, "", $NF); print $NF; exit }')
        else
          hostname=""
        fi
        hostname="${hostname:-$remote_ip}"
        echo "${total}|${proc}|${hostname}|${bytes_in}|${bytes_out}|${retx}"
      done |
      awk -F"|" '
        function human(b) {
          if (b >= 1073741824) return sprintf("%.1f GB", b / 1073741824)
          if (b >= 1048576) return sprintf("%.1f MB", b / 1048576)
          if (b >= 1024) return sprintf("%.1f KB", b / 1024)
          return b " B"
        }
        NR == 1 { printf "  %-50s %-50s %10s %10s %8s\n", "Process", "Remote Host", "Recv", "Sent", "Re-TX" }
        {
          p = $2
          if (length(p) > 50) p = substr(p, 1, 47) "..."
          h = $3
          if (length(h) > 50) h = substr(h, 1, 47) "..."
          printf "  %-50s %-50s %10s %10s", p, h, human($4), human($5)
          if ($6 > 0) printf " %8d", $6; else printf " %8s", "-"
          printf "\n"
        }
      '
    echo
  fi

  # Problem detection
  _section "Issues Detected"
  local issues=0

  if [[ "$main_available" -eq 1 ]]; then
    local spikes
    spikes=$(awk -F, 'NR > 1 && $14 + 0 > 100 { print $1 ": " $14 "ms" }' "$main_file")
    if [[ -n "$spikes" ]]; then
      echo "  ! High latency spikes (>100ms):"
      echo "$spikes" | head -5 | sed "s/^/      /"
      local spike_count
      spike_count=$(echo "$spikes" | wc -l | tr -d " ")
      [[ "$spike_count" -gt 5 ]] && echo "      ... and $((spike_count - 5)) more"
      issues=$((issues + 1))
    fi

    local loss_events
    loss_events=$(awk -F, 'NR > 1 && $12 + 0 > 0 { print $1 ": " $12 "% loss" }' "$main_file")
    if [[ -n "$loss_events" ]]; then
      echo "  ! Packet loss events:"
      echo "$loss_events" | head -5 | sed "s/^/      /"
      local loss_count
      loss_count=$(echo "$loss_events" | wc -l | tr -d " ")
      [[ "$loss_count" -gt 5 ]] && echo "      ... and $((loss_count - 5)) more"
      issues=$((issues + 1))
    fi

    local weak_signal
    weak_signal=$(awk -F, 'NR > 1 && $4 ~ /^-/ && $4 + 0 < -75 { print $1 ": " $4 " dBm" }' "$main_file")
    if [[ -n "$weak_signal" ]]; then
      echo "  ! Weak Wi-Fi signal (<-75 dBm):"
      echo "$weak_signal" | head -5 | sed "s/^/      /"
      issues=$((issues + 1))
    fi

    local slow_dns
    slow_dns=$(awk -F, 'NR > 1 && $16 + 0 > 200 { print $1 ": " $16 "ms" }' "$main_file")
    if [[ -n "$slow_dns" ]]; then
      echo "  ! Slow DNS lookups (>200ms):"
      echo "$slow_dns" | head -5 | sed "s/^/      /"
      issues=$((issues + 1))
    fi
  fi

  if [[ -f "$traffic_file" ]]; then
    local high_retx
    high_retx=$(awk -F, '
      NR == 1 { next }
      { retx[$2] += $8 }
      END {
        for (proc in retx)
          if (retx[proc] > 50) printf "      %s: %d retransmits\n", proc, retx[proc]
      }
    ' "$traffic_file")
    if [[ -n "$high_retx" ]]; then
      echo "  ! High TCP retransmits (>50):"
      echo "$high_retx" | head -5
      issues=$((issues + 1))
    fi
  fi

  if [[ "$issues" -eq 0 ]]; then
    echo "  OK No significant issues detected."
  fi

  echo
  print_rule
  echo " Raw CSV     : $sample_file"
  [[ "$requested" != "$sample_file" ]] && echo " Input CSV   : $requested"
  [[ -f "$traffic_file" ]] && echo " Traffic CSV : $traffic_file"
  [[ -f "$conn_file" ]] && echo " Connect CSV : $conn_file"
  print_rule
}

cmd_list() {
  ensure_log_dir
  echo "Available logs:"
  local file found=0 size mtime

  shopt -s nullglob
  for file in "$LOG_DIR"/call-*.csv; do
    [[ "$file" == *-traffic.csv ]] && continue
    [[ "$file" == *-connections.csv ]] && continue
    found=1
    size=$(wc -c <"$file" | tr -d " ")
    mtime=$(stat -f "%Sm" -t "%Y-%m-%d %H:%M:%S" "$file" 2>/dev/null || echo "unknown-time")
    printf "  %s  (%s bytes, %s)\n" "$file" "$size" "$mtime"
  done
  shopt -u nullglob

  [[ "$found" -eq 0 ]] && echo "  (none)"
}

# -- main ------------------------------------------------------------

case "${1:-help}" in
start)
  cmd_start
  ;;
stop)
  cmd_stop
  ;;
monitor)
  shift
  cmd_monitor "$@"
  ;;
measure)
  shift
  cmd_measure "$@"
  ;;
review)
  cmd_review "${2:-}"
  ;;
list)
  cmd_list
  ;;
help | *)
  echo "netmon.sh - log network conditions during calls"
  echo
  echo "Usage:"
  echo "  $0 start           Start monitoring (runs in background)"
  echo "  $0 stop            Stop monitoring"
  echo "  $0 monitor         Start (or attach) and show live ncurses dashboard"
  echo "  $0 measure         Measure interface throughput for a time window"
  echo "  $0 review [file]   Review latest (or specified) log"
  echo "  $0 list            List all log files"
  echo
  echo "Monitor options:"
  echo "  $0 monitor --attach       Attach to existing collector only"
  echo "  $0 monitor --keep-running Keep collector running after quitting TUI"
  echo
  echo "Measure options:"
  echo "  $0 measure --duration 60 --interface en0"
  echo
  echo "Environment variables:"
  echo "  MONITOR_INTERVAL   Seconds between samples (default: 2)"
  echo "  PING_TARGET        Host to ping (default: 8.8.8.8)"
  echo "  PING_COUNT         Echo requests per sample (default: 3)"
  echo "  PING_TIMEOUT_MS    Ping timeout in milliseconds (default: 2000)"
  ;;
esac
