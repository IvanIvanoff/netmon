# lib/config.sh — constants, CSV headers, environment variable defaults.

[[ -n "${_NETMON_CONFIG_LOADED:-}" ]] && return 0
_NETMON_CONFIG_LOADED=1

LOG_DIR="${LOG_DIR:-$HOME/call-network-logs}"
PID_FILE="$LOG_DIR/.monitor.pid"

INTERVAL="${MONITOR_INTERVAL:-2}" # seconds between samples
PING_TARGET="${PING_TARGET:-8.8.8.8}"
PING_COUNT="${PING_COUNT:-3}"
PING_TIMEOUT_MS="${PING_TIMEOUT_MS:-2000}" # macOS ping uses milliseconds

REPORT_WIDTH=140

AIRPORT_PATH="/System/Library/PrivateFrameworks/Apple80211.framework/Versions/Current/Resources/airport"
WIFI_HELPER="$LOG_DIR/.wifi_helper"

# Resolve project root (one level above lib/)
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
MONITOR_TUI_PY="$SCRIPT_DIR/netmon_tui.py"

MAIN_CSV_HEADER="timestamp,ssid,channel,rssi_dBm,noise_dBm,snr_dB,tx_rate_Mbps,interface,local_ip,public_ip,ping_target,loss_%,ping_min_ms,ping_avg_ms,ping_max_ms,dns_ms,gateway_ip,gw_ping_ms,jitter_ms,bssid,mcs,channel_band,channel_width,if_ierrs,if_oerrs,cpu_usage,mem_pressure,awdl_status,cca_pct"
TRAFFIC_CSV_HEADER="sample_ts,process,pid,bytes_in,bytes_out,rx_dupe,rx_ooo,retransmits"
CONNECTIONS_CSV_HEADER="sample_ts,process,pid,remote_ip,remote_port,bytes_in,bytes_out,retransmits"
SCAN_CSV_HEADER="scan_ts,ssid,bssid,rssi,channel,security"
UDP_CSV_HEADER="sample_ts,process,pid,bytes_in,bytes_out"
SCAN_INTERVAL=15  # run wifi scan every N sample cycles
