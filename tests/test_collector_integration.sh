#!/usr/bin/env bash
#
# test_collector_integration.sh
#
# Integration test: starts the collector, lets it run for a few cycles,
# stops it, and validates the CSV output.
#
# Usage: bash tests/test_collector_integration.sh
# Requires: macOS (uses real system tools)
# Runtime: ~20 seconds

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
NETMON="$SCRIPT_DIR/../netmon.sh"

PASS=0
FAIL=0
ERRORS=""
TEST_LOG_DIR=""
COLLECTOR_PID=""

assert_eq() {
  local test_name="$1" expected="$2" actual="$3"
  if [[ "$expected" == "$actual" ]]; then
    PASS=$((PASS + 1))
  else
    FAIL=$((FAIL + 1))
    ERRORS+="  FAIL: $test_name\n    expected: '$expected'\n    actual:   '$actual'\n\n"
  fi
}

assert_gt() {
  local test_name="$1" threshold="$2" actual="$3"
  if (( actual > threshold )); then
    PASS=$((PASS + 1))
  else
    FAIL=$((FAIL + 1))
    ERRORS+="  FAIL: $test_name\n    expected > $threshold, got: $actual\n\n"
  fi
}

assert_true() {
  local test_name="$1" condition="$2"
  if [[ "$condition" == "true" || "$condition" == "1" || "$condition" == "yes" ]]; then
    PASS=$((PASS + 1))
  else
    FAIL=$((FAIL + 1))
    ERRORS+="  FAIL: $test_name\n    condition was false\n\n"
  fi
}

cleanup() {
  if [[ -n "$TEST_LOG_DIR" ]]; then
    local pid_file="$TEST_LOG_DIR/.monitor.pid"
    if [[ -f "$pid_file" ]]; then
      local pid
      pid=$(cat "$pid_file" 2>/dev/null || true)
      if [[ -n "$pid" ]] && kill -0 "$pid" 2>/dev/null; then
        kill "$pid" 2>/dev/null || true
        sleep 0.5
        kill -9 "$pid" 2>/dev/null || true
      fi
    fi
    if [[ -n "$COLLECTOR_PID" ]] && kill -0 "$COLLECTOR_PID" 2>/dev/null; then
      kill "$COLLECTOR_PID" 2>/dev/null || true
      sleep 0.5
      kill -9 "$COLLECTOR_PID" 2>/dev/null || true
    fi
    rm -rf "$TEST_LOG_DIR"
  fi
}
trap cleanup EXIT

# ===================================================================
# Setup
# ===================================================================

TEST_LOG_DIR=$(mktemp -d)
export LOG_DIR="$TEST_LOG_DIR"
export MONITOR_INTERVAL=2
OUT_FILE=$(mktemp)

echo "=== Integration Test ==="
echo "Log dir: $TEST_LOG_DIR"
echo ""

# ===================================================================
# Test: start / stop lifecycle
# ===================================================================

echo "--- Lifecycle tests ---"

# Start collector.
# IMPORTANT: do NOT use output=$(...) — the subshell captures the backgrounded
# sample_loop's inherited file descriptors and blocks forever.
# Instead, redirect to a file.
LOG_DIR="$TEST_LOG_DIR" "$NETMON" start >"$OUT_FILE" 2>&1
start_rc=$?
assert_true "start: exit 0" "$([[ $start_rc -eq 0 ]] && echo true || echo false)"
assert_true "start: PID file created" "$([[ -f "$TEST_LOG_DIR/.monitor.pid" ]] && echo true || echo false)"

pid=$(cat "$TEST_LOG_DIR/.monitor.pid" 2>/dev/null || echo "")
COLLECTOR_PID="$pid"
assert_true "start: PID is numeric" "$([[ "$pid" =~ ^[0-9]+$ ]] && echo true || echo false)"
assert_true "start: process running" "$(kill -0 "$pid" 2>/dev/null && echo true || echo false)"

# Double start should say already running
LOG_DIR="$TEST_LOG_DIR" "$NETMON" start >"$OUT_FILE" 2>&1
output=$(cat "$OUT_FILE")
assert_true "start: already running msg" "$([[ "$output" == *"already running"* || "$output" == *"Already running"* ]] && echo true || echo false)"

# Let it collect some samples
echo "Collecting samples for ~12 seconds..."
sleep 12

# Find the log files
main_csv=$(ls "$TEST_LOG_DIR"/call-*.csv 2>/dev/null | grep -v traffic | grep -v connections | grep -v scan | head -1)
assert_true "main CSV exists" "$([[ -n "$main_csv" && -f "$main_csv" ]] && echo true || echo false)"

if [[ -z "$main_csv" || ! -f "$main_csv" ]]; then
  echo "FATAL: No main CSV found, cannot continue."
  echo "Files in log dir:"
  ls -la "$TEST_LOG_DIR"/ 2>/dev/null || true
  echo ""
  printf "$ERRORS"
  rm -f "$OUT_FILE"
  exit 1
fi

traffic_csv="${main_csv%.csv}-traffic.csv"
conn_csv="${main_csv%.csv}-connections.csv"
scan_csv="${main_csv%.csv}-scan.csv"

assert_true "traffic CSV exists" "$([[ -f "$traffic_csv" ]] && echo true || echo false)"
assert_true "connections CSV exists" "$([[ -f "$conn_csv" ]] && echo true || echo false)"
assert_true "scan CSV exists" "$([[ -f "$scan_csv" ]] && echo true || echo false)"

# Stop collector
LOG_DIR="$TEST_LOG_DIR" "$NETMON" stop >"$OUT_FILE" 2>&1
sleep 1
assert_true "stop: PID file removed" "$([[ ! -f "$TEST_LOG_DIR/.monitor.pid" ]] && echo true || echo false)"
assert_true "stop: process gone" "$(! kill -0 "$pid" 2>/dev/null && echo true || echo false)"

# Double stop should be fine
LOG_DIR="$TEST_LOG_DIR" "$NETMON" stop >"$OUT_FILE" 2>&1
output=$(cat "$OUT_FILE")
assert_true "stop: no monitor msg" "$([[ "$output" == *"No monitor"* ]] && echo true || echo false)"

# ===================================================================
# Test: CSV format validation
# ===================================================================

echo ""
echo "--- CSV format validation ---"

EXPECTED_HEADER="timestamp,ssid,channel,rssi_dBm,noise_dBm,snr_dB,tx_rate_Mbps,interface,local_ip,public_ip,ping_target,loss_%,ping_min_ms,ping_avg_ms,ping_max_ms,dns_ms,gateway_ip,gw_ping_ms,jitter_ms,bssid,mcs,channel_band,channel_width,if_ierrs,if_oerrs,cpu_usage,mem_pressure"
actual_header=$(head -1 "$main_csv")
assert_eq "main CSV: correct header" "$EXPECTED_HEADER" "$actual_header"

expected_cols=$(echo "$EXPECTED_HEADER" | awk -F, '{print NF}')
assert_eq "main CSV: header has 27 columns" "27" "$expected_cols"

# Check every data row has correct column count
line_num=0
col_errors=0
while IFS= read -r line; do
  line_num=$((line_num + 1))
  [[ $line_num -eq 1 ]] && continue
  cols=$(echo "$line" | awk -F, '{print NF}')
  if [[ "$cols" != "$expected_cols" ]]; then
    col_errors=$((col_errors + 1))
    if [[ $col_errors -le 3 ]]; then
      echo "  Column mismatch on line $line_num: expected $expected_cols, got $cols"
      echo "  Line: $line"
    fi
  fi
done < "$main_csv"
assert_eq "main CSV: all rows have 27 columns" "0" "$col_errors"

data_lines=$((line_num - 1))
assert_gt "main CSV: has samples" 2 "$data_lines"
echo "  Collected $data_lines samples"

traffic_header=$(head -1 "$traffic_csv")
assert_eq "traffic CSV: correct header" "sample_ts,process,pid,bytes_in,bytes_out,rx_dupe,rx_ooo,retransmits" "$traffic_header"

conn_header=$(head -1 "$conn_csv")
assert_eq "connections CSV: correct header" "sample_ts,process,pid,remote_ip,remote_port,bytes_in,bytes_out,retransmits" "$conn_header"

scan_header=$(head -1 "$scan_csv")
assert_eq "scan CSV: correct header" "scan_ts,ssid,bssid,rssi,channel,security" "$scan_header"

# ===================================================================
# Test: data quality in main CSV
# ===================================================================

echo ""
echo "--- Data quality checks ---"

bad_ts=$(awk -F, 'NR > 1 && $1 !~ /^[0-9]{4}-[0-9]{2}-[0-9]{2} [0-9]{2}:[0-9]{2}:[0-9]{2}$/ { print NR": "$1 }' "$main_csv")
assert_eq "main CSV: all timestamps valid" "" "$bad_ts"

iface=$(awk -F, 'NR == 2 { print $8 }' "$main_csv")
assert_true "main CSV: interface not empty" "$([[ -n "$iface" && "$iface" != "unknown" ]] && echo true || echo false)"

lip=$(awk -F, 'NR == 2 { print $9 }' "$main_csv")
assert_true "main CSV: local IP looks valid" "$([[ "$lip" =~ ^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+$ ]] && echo true || echo false)"

ping_target=$(awk -F, 'NR == 2 { print $11 }' "$main_csv")
assert_eq "main CSV: ping target" "8.8.8.8" "$ping_target"

cpu=$(awk -F, 'NR == 2 { print $26 }' "$main_csv")
assert_true "main CSV: CPU is numeric" "$([[ "$cpu" =~ ^[0-9]+$ ]] && echo true || echo false)"

mem=$(awk -F, 'NR == 2 { print $27 }' "$main_csv")
assert_true "main CSV: memory is numeric" "$([[ "$mem" =~ ^[0-9]+$ ]] && echo true || echo false)"

gw=$(awk -F, 'NR == 2 { print $17 }' "$main_csv")
assert_true "main CSV: gateway looks like IP" "$([[ "$gw" =~ ^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+$ || "$gw" == "?" ]] && echo true || echo false)"

loss_val=$(awk -F, 'NR == 2 { print $12 }' "$main_csv")
assert_true "main CSV: loss is numeric" "$([[ "$loss_val" =~ ^[0-9.]+$ || "$loss_val" == "?" ]] && echo true || echo false)"

rssi=$(awk -F, 'NR == 2 { print $4 }' "$main_csv")
assert_true "main CSV: RSSI is negative or ?" "$([[ "$rssi" =~ ^-[0-9]+$ || "$rssi" == "?" ]] && echo true || echo false)"

band=$(awk -F, 'NR == 2 { print $22 }' "$main_csv")
assert_true "main CSV: band valid" "$([[ "$band" == "2.4" || "$band" == "5" || "$band" == "?" ]] && echo true || echo false)"

# ===================================================================
# Test: traffic CSV has data
# ===================================================================

echo ""
echo "--- Traffic data checks ---"

traffic_lines=$(($(wc -l < "$traffic_csv") - 1))
assert_gt "traffic CSV: has data rows" 0 "$traffic_lines"
echo "  Traffic rows: $traffic_lines"

bad_traffic=$(awk -F, 'NR > 1 && ($4 !~ /^[0-9]+$/ || $5 !~ /^[0-9]+$/) { print NR }' "$traffic_csv" | head -3)
assert_eq "traffic CSV: bytes are numeric" "" "$bad_traffic"

# ===================================================================
# Test: end-to-end pipeline (collector CSV → Python parser → diagnostics)
# ===================================================================

echo ""
echo "--- End-to-end pipeline test ---"

pipeline_result=$(python3 -c "
import sys
sys.path.insert(0, '$SCRIPT_DIR/..')
from netmon_tui import parse_main_csv, run_diagnostics, parse_scan_csv
from pathlib import Path

main = parse_main_csv(Path('$main_csv'))
print(f'samples={main[\"samples\"]}')
print(f'first_ts={main[\"first_ts\"]}')
print(f'ping_count={len(main[\"ping_vals\"])}')
print(f'rssi_count={len(main[\"rssi_vals\"])}')
print(f'cpu_count={len(main[\"cpu_vals\"])}')
print(f'mem_count={len(main[\"mem_vals\"])}')

scan = parse_scan_csv(Path('$scan_csv'))
print(f'scan_rows={len(scan)}')

diag = run_diagnostics(main, scan)
print(f'diag_count={len(diag)}')
for sev, msg in diag:
    print(f'  [{sev}] {msg}')
" 2>&1)

echo "$pipeline_result"

py_samples=$(echo "$pipeline_result" | awk -F= '/^samples=/ { print $2 }')
assert_eq "pipeline: Python sample count matches" "$data_lines" "$py_samples"

diag_count=$(echo "$pipeline_result" | awk -F= '/^diag_count=/ { print $2 }')
assert_gt "pipeline: diagnostics produced results" 0 "$diag_count"

ping_count=$(echo "$pipeline_result" | awk -F= '/^ping_count=/ { print $2 }')
assert_gt "pipeline: ping values parsed" 0 "$ping_count"

cpu_count=$(echo "$pipeline_result" | awk -F= '/^cpu_count=/ { print $2 }')
assert_gt "pipeline: CPU values parsed" 0 "$cpu_count"

# ===================================================================
# Summary
# ===================================================================

rm -f "$OUT_FILE"

echo ""
echo "================================"
echo " Integration tests: $PASS passed, $FAIL failed"
echo "================================"

if [[ "$FAIL" -gt 0 ]]; then
  echo ""
  printf "$ERRORS"
  exit 1
fi
