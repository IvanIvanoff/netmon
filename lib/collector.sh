# lib/collector.sh — the core sample_loop that drives data collection.

[[ -n "${_NETMON_COLLECTOR_LOADED:-}" ]] && return 0
_NETMON_COLLECTOR_LOADED=1

[[ -n "${_NETMON_CONFIG_LOADED:-}" ]]  || source "$(dirname "${BASH_SOURCE[0]}")/config.sh"
[[ -n "${_NETMON_HELPERS_LOADED:-}" ]] || source "$(dirname "${BASH_SOURCE[0]}")/helpers.sh"
[[ -n "${_NETMON_WIFI_LOADED:-}" ]]    || source "$(dirname "${BASH_SOURCE[0]}")/wifi.sh"
[[ -n "${_NETMON_MEASURE_LOADED:-}" ]] || source "$(dirname "${BASH_SOURCE[0]}")/measure.sh"
[[ -n "${_NETMON_SYSTEM_LOADED:-}" ]]  || source "$(dirname "${BASH_SOURCE[0]}")/system.sh"
[[ -n "${_NETMON_TRAFFIC_LOADED:-}" ]] || source "$(dirname "${BASH_SOURCE[0]}")/traffic.sh"

sample_loop() {
  # Disable errexit: the monitor must survive transient failures
  # (e.g. WiFi drops during network switch)
  set +e

  local logfile="$1" traffic_file="$2" conn_file="$3" scan_file="$4" udp_file="$5"

  echo "$MAIN_CSV_HEADER" >"$logfile"
  echo "$TRAFFIC_CSV_HEADER" >"$traffic_file"
  echo "$CONNECTIONS_CSV_HEADER" >"$conn_file"
  echo "$SCAN_CSV_HEADER" >"$scan_file"
  echo "$UDP_CSV_HEADER" >"$udp_file"
  echo "$DIAG_CSV_HEADER" >"$(dirname "$logfile")/diagnostics.csv"

  local pub_ip
  pub_ip=$(get_public_ip)
  pub_ip="${pub_ip:-?}"

  local ping_file dns_file gw_ping_file prev_traffic curr_traffic prev_conn curr_conn prev_udp curr_udp name_file ext_file
  ping_file=$(make_tmp_file "ping")
  dns_file=$(make_tmp_file "dns")
  gw_ping_file=$(make_tmp_file "gwping")
  prev_traffic=$(make_tmp_file "tprev")
  curr_traffic=$(make_tmp_file "tcurr")
  prev_conn=$(make_tmp_file "cprev")
  curr_conn=$(make_tmp_file "ccurr")
  prev_udp=$(make_tmp_file "uprev")
  curr_udp=$(make_tmp_file "ucurr")
  name_file=$(make_tmp_file "names")
  ext_file=$(make_tmp_file "ext")

  # shellcheck disable=SC2064
  trap "rm -f '$ping_file' '$dns_file' '$gw_ping_file' '$prev_traffic' '$curr_traffic' '$prev_conn' '$curr_conn' '$prev_udp' '$curr_udp' '$name_file' '$ext_file'" EXIT INT TERM

  # Baseline snapshots (not logged; used as zero point)
  _nettop_snapshot >"$prev_traffic" || : >"$prev_traffic"
  _nettop_conn_snapshot >"$prev_conn" || : >"$prev_conn"
  _nettop_udp_snapshot >"$prev_udp" || : >"$prev_udp"

  # Baseline interface errors (read actual counters so first sample delta is 0)
  local prev_ierrs=0 prev_oerrs=0
  local baseline_iface
  baseline_iface=$(get_active_interface || echo "unknown")
  if [[ -n "$baseline_iface" && "$baseline_iface" != "unknown" ]]; then
    local baseline_errs
    baseline_errs=$(get_interface_errors "$baseline_iface" || echo "0|0")
    IFS="|" read -r prev_ierrs prev_oerrs <<<"$baseline_errs"
    prev_ierrs="${prev_ierrs:-0}"
    prev_oerrs="${prev_oerrs:-0}"
  fi
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
    capture_udp_traffic "$ts" "$udp_file" "$prev_udp" "$curr_udp" "$name_file" || true

    # System metrics (fast, no background needed)
    cpu_usage=$(get_cpu_usage)
    cpu_usage="${cpu_usage:-?}"
    mem_pressure=$(get_mem_pressure)
    mem_pressure="${mem_pressure:-?}"
    local awdl_status
    awdl_status=$(get_awdl_status)
    local cca_pct
    cca_pct=$(get_cca_percent)

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

    printf "%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s\n" \
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
      "$(sanitize_csv_field "$mem_pressure")" \
      "$(sanitize_csv_field "$awdl_status")" \
      "$(sanitize_csv_field "$cca_pct")" >>"$logfile"

    sleep "$INTERVAL"
  done
}
