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

# Source library modules
LIB_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/lib" && pwd)"
source "$LIB_DIR/config.sh"
source "$LIB_DIR/helpers.sh"
source "$LIB_DIR/process.sh"
source "$LIB_DIR/wifi.sh"
source "$LIB_DIR/measure.sh"
source "$LIB_DIR/system.sh"
source "$LIB_DIR/traffic.sh"
source "$LIB_DIR/collector.sh"
source "$LIB_DIR/report.sh"

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

  local stamp session_dir logfile traffic_file conn_file scan_file udp_file
  stamp=$(date +%Y%m%d-%H%M%S)
  session_dir="$LOG_DIR/call-${stamp}"
  mkdir -p "$session_dir"
  logfile="$session_dir/main.csv"
  traffic_file="$session_dir/traffic.csv"
  conn_file="$session_dir/connections.csv"
  scan_file="$session_dir/scan.csv"
  udp_file="$session_dir/udp.csv"

  echo "Starting network monitor..."
  echo "  Session  : $session_dir"
  echo "  Log file : $logfile"
  echo "  Traffic  : $traffic_file"
  echo "  Connects : $conn_file"
  echo "  UDP      : $udp_file"
  echo "  WiFi scan: $scan_file"
  echo "  Interval : ${INTERVAL}s"
  echo "  Ping host: $PING_TARGET"

  sample_loop "$logfile" "$traffic_file" "$conn_file" "$scan_file" "$udp_file" &
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
    local samples session_dir
    samples=$(($(wc -l <"$latest") - 1))
    session_dir=$(dirname "$latest")
    echo "Logged $samples samples to: $session_dir/"
  fi
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
chart)
  shift
  has_cmd python3 || die "python3 is required for chart generation."
  python3 "$SCRIPT_DIR/netmon_chart.py" --log-dir "$LOG_DIR" "$@"
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
  echo "  $0 chart           Open interactive diagnostics timeline in browser"
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
