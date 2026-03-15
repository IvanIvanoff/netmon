#!/usr/bin/env bash
#
# test_bash_parsers.sh
#
# Tests for bash parser functions in netmon lib/ modules.
# Sources the library files directly and feeds them canned input.
#
# Usage: bash tests/test_bash_parsers.sh
# Exit code 0 = all pass, non-zero = failures.

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LIB_DIR="$SCRIPT_DIR/../lib"

PASS=0
FAIL=0
ERRORS=""

# -- test helpers -----------------------------------------------------------

assert_eq() {
  local test_name="$1" expected="$2" actual="$3"
  if [[ "$expected" == "$actual" ]]; then
    PASS=$((PASS + 1))
  else
    FAIL=$((FAIL + 1))
    ERRORS+="  FAIL: $test_name\n    expected: '$expected'\n    actual:   '$actual'\n\n"
  fi
}

assert_contains() {
  local test_name="$1" needle="$2" haystack="$3"
  if [[ "$haystack" == *"$needle"* ]]; then
    PASS=$((PASS + 1))
  else
    FAIL=$((FAIL + 1))
    ERRORS+="  FAIL: $test_name\n    expected to contain: '$needle'\n    actual: '$haystack'\n\n"
  fi
}

# -- source library modules -------------------------------------------------

set +e  # tests need to run even if individual commands fail
source "$LIB_DIR/config.sh"
source "$LIB_DIR/helpers.sh"
source "$LIB_DIR/wifi.sh"
source "$LIB_DIR/measure.sh"

# ===================================================================
# parse_ping tests
# ===================================================================

echo "=== parse_ping ==="

# Normal ping output (macOS format)
PING_NORMAL="PING 8.8.8.8 (8.8.8.8): 56 data bytes
64 bytes from 8.8.8.8: icmp_seq=0 ttl=118 time=12.145 ms
64 bytes from 8.8.8.8: icmp_seq=1 ttl=118 time=11.823 ms
64 bytes from 8.8.8.8: icmp_seq=2 ttl=118 time=13.456 ms

--- 8.8.8.8 ping statistics ---
3 packets transmitted, 3 packets received, 0.0% packet loss
round-trip min/avg/max/stddev = 11.823/12.475/13.456/0.683 ms"

result=$(parse_ping "$PING_NORMAL")
IFS="|" read -r loss pmin pavg pmax <<< "$result"
assert_eq "parse_ping normal: loss" "0.0" "$loss"
assert_eq "parse_ping normal: min" "11.823" "$pmin"
assert_eq "parse_ping normal: avg" "12.475" "$pavg"
assert_eq "parse_ping normal: max" "13.456" "$pmax"

# Partial loss
PING_LOSS="PING 8.8.8.8 (8.8.8.8): 56 data bytes
64 bytes from 8.8.8.8: icmp_seq=0 ttl=118 time=12.145 ms

--- 8.8.8.8 ping statistics ---
3 packets transmitted, 2 packets received, 33.3% packet loss
round-trip min/avg/max/stddev = 12.145/12.145/12.145/0.000 ms"

result=$(parse_ping "$PING_LOSS")
IFS="|" read -r loss pmin pavg pmax <<< "$result"
assert_eq "parse_ping loss: loss" "33.3" "$loss"

# 100% loss
PING_TOTAL_LOSS="PING 8.8.8.8 (8.8.8.8): 56 data bytes

--- 8.8.8.8 ping statistics ---
3 packets transmitted, 0 packets received, 100.0% packet loss"

result=$(parse_ping "$PING_TOTAL_LOSS")
IFS="|" read -r loss pmin pavg pmax <<< "$result"
assert_eq "parse_ping total loss: loss" "100.0" "$loss"
assert_eq "parse_ping total loss: min" "?" "$pmin"
assert_eq "parse_ping total loss: avg" "?" "$pavg"

# Empty input
result=$(parse_ping "")
IFS="|" read -r loss pmin pavg pmax <<< "$result"
assert_eq "parse_ping empty: loss" "?" "$loss"

# ===================================================================
# parse_wifi_info tests
# ===================================================================

echo "=== parse_wifi_info ==="

# Airport-style output
AIRPORT_OUTPUT="     agrCtlRSSI: -52
     agrCtlNoise: -95
          SSID: MyNetwork
       channel: 36
     lastTxRate: 800
         BSSID: aa:bb:cc:dd:ee:ff
           MCS: 9
  channelWidth: 80"

result=$(echo "$AIRPORT_OUTPUT" | parse_wifi_info)
IFS="|" read -r ssid channel rssi noise tx_rate bssid mcs ch_width <<< "$result"
assert_eq "wifi_info: ssid" "MyNetwork" "$ssid"
assert_eq "wifi_info: channel" "36" "$channel"
assert_eq "wifi_info: rssi" "-52" "$rssi"
assert_eq "wifi_info: noise" "-95" "$noise"
assert_eq "wifi_info: tx_rate" "800" "$tx_rate"
assert_eq "wifi_info: bssid" "aa:bb:cc:dd:ee:ff" "$bssid"
assert_eq "wifi_info: mcs" "9" "$mcs"
assert_eq "wifi_info: channel_width" "80" "$ch_width"

# Missing fields
WIFI_PARTIAL="     agrCtlRSSI: -65
     agrCtlNoise: -90
          SSID: TestNet
       channel: 11"

result=$(echo "$WIFI_PARTIAL" | parse_wifi_info)
IFS="|" read -r ssid channel rssi noise tx_rate bssid mcs ch_width <<< "$result"
assert_eq "wifi_partial: ssid" "TestNet" "$ssid"
assert_eq "wifi_partial: channel" "11" "$channel"
assert_eq "wifi_partial: rssi" "-65" "$rssi"
assert_eq "wifi_partial: tx_rate" "?" "$tx_rate"
assert_eq "wifi_partial: bssid" "?" "$bssid"

# Empty input
result=$(echo "" | parse_wifi_info)
IFS="|" read -r ssid channel rssi noise tx_rate bssid mcs ch_width <<< "$result"
assert_eq "wifi_empty: ssid" "unknown" "$ssid"
assert_eq "wifi_empty: rssi" "?" "$rssi"

# ===================================================================
# channel_to_band tests
# ===================================================================

echo "=== channel_to_band ==="

assert_eq "ch1 -> 2.4" "2.4" "$(channel_to_band 1)"
assert_eq "ch6 -> 2.4" "2.4" "$(channel_to_band 6)"
assert_eq "ch11 -> 2.4" "2.4" "$(channel_to_band 11)"
assert_eq "ch14 -> 2.4" "2.4" "$(channel_to_band 14)"
assert_eq "ch36 -> 5" "5" "$(channel_to_band 36)"
assert_eq "ch44 -> 5" "5" "$(channel_to_band 44)"
assert_eq "ch149 -> 5" "5" "$(channel_to_band 149)"
assert_eq "ch177 -> 5" "5" "$(channel_to_band 177)"
assert_eq "ch0 -> ?" "?" "$(channel_to_band 0)"
assert_eq "ch200 -> ?" "?" "$(channel_to_band 200)"
assert_eq "invalid -> ?" "?" "$(channel_to_band abc)"
assert_eq "empty -> ?" "?" "$(channel_to_band "")"

# ===================================================================
# sanitize_csv_field tests
# ===================================================================

echo "=== sanitize_csv_field ==="

assert_eq "sanitize: plain" "hello" "$(sanitize_csv_field "hello")"
assert_eq "sanitize: comma" "hello;world" "$(sanitize_csv_field "hello,world")"
assert_eq "sanitize: empty" "" "$(sanitize_csv_field "")"
assert_eq "sanitize: number" "42" "$(sanitize_csv_field "42")"
assert_eq "sanitize: multiple commas" "a;b;c" "$(sanitize_csv_field "a,b,c")"
# Channel format that caused the original bug
assert_eq "sanitize: channel" "36 (5GHz; 80MHz)" "$(sanitize_csv_field "36 (5GHz, 80MHz)")"

# ===================================================================
# parse_jitter tests
# ===================================================================

echo "=== parse_jitter ==="

# Normal 3-ping output
JITTER_INPUT="PING 8.8.8.8 (8.8.8.8): 56 data bytes
64 bytes from 8.8.8.8: icmp_seq=0 ttl=118 time=10.0 ms
64 bytes from 8.8.8.8: icmp_seq=1 ttl=118 time=20.0 ms
64 bytes from 8.8.8.8: icmp_seq=2 ttl=118 time=15.0 ms"

# mean = 15.0, deviations: |10-15|=5, |20-15|=5, |15-15|=0, mean_dev = 10/3 ≈ 3.3
result=$(parse_jitter "$JITTER_INPUT")
assert_contains "jitter: 3 pings" "3.3" "$result"

# Single ping → not enough data
JITTER_ONE="64 bytes from 8.8.8.8: icmp_seq=0 ttl=118 time=10.0 ms"
result=$(parse_jitter "$JITTER_ONE")
assert_eq "jitter: 1 ping" "?" "$result"

# Identical times → zero jitter
JITTER_SAME="64 bytes from 8.8.8.8: icmp_seq=0 ttl=118 time=10.0 ms
64 bytes from 8.8.8.8: icmp_seq=1 ttl=118 time=10.0 ms
64 bytes from 8.8.8.8: icmp_seq=2 ttl=118 time=10.0 ms"
result=$(parse_jitter "$JITTER_SAME")
assert_eq "jitter: identical" "0.0" "$result"

# Empty input
result=$(parse_jitter "")
assert_eq "jitter: empty" "?" "$result"

# ===================================================================
# Summary
# ===================================================================

echo ""
echo "================================"
echo " Bash parser tests: $PASS passed, $FAIL failed"
echo "================================"

if [[ "$FAIL" -gt 0 ]]; then
  echo ""
  printf "$ERRORS"
  exit 1
fi
