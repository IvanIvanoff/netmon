# lib/report.sh — post-session report rendering (cmd_review, cmd_list).

[[ -n "${_NETMON_REPORT_LOADED:-}" ]] && return 0
_NETMON_REPORT_LOADED=1

[[ -n "${_NETMON_CONFIG_LOADED:-}" ]]  || source "$(dirname "${BASH_SOURCE[0]}")/config.sh"
[[ -n "${_NETMON_HELPERS_LOADED:-}" ]] || source "$(dirname "${BASH_SOURCE[0]}")/helpers.sh"

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

  # UDP Traffic
  local udp_file="${main_file%.csv}-udp.csv"
  if [[ -f "$udp_file" ]] && [[ $(wc -l <"$udp_file") -gt 1 ]]; then
    _section "Per-Process UDP Traffic"
    awk -F, '
      NR == 1 { next }
      {
        in_sum[$2] += $4
        out_sum[$2] += $5
      }
      END {
        for (proc in in_sum) {
          total = in_sum[proc] + out_sum[proc]
          if (total > 0) printf "%d|%s|%d|%d\n", total, proc, in_sum[proc], out_sum[proc]
        }
      }
    ' "$udp_file" | sort -t"|" -k1 -nr | head -10 |
      awk -F"|" '
        function human(b) {
          if (b >= 1073741824) return sprintf("%.1f GB", b / 1073741824)
          if (b >= 1048576) return sprintf("%.1f MB", b / 1048576)
          if (b >= 1024) return sprintf("%.1f KB", b / 1024)
          return b " B"
        }
        NR == 1 { printf "  %-105s %10s %10s\n", "Process", "Recv", "Sent" }
        {
          name = $2
          if (length(name) > 105) name = substr(name, 1, 102) "..."
          printf "  %-105s %10s %10s\n", name, human($3), human($4)
        }
      '
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

  # Recommendations based on detected issues
  _section "Recommendations"
  local recs=0

  if [[ "$main_available" -eq 1 ]]; then
    # DFS channel
    local ch
    ch=$(awk -F, 'NR == 2 { print $3 }' "$main_file")
    if [[ "$ch" =~ ^[0-9]+$ ]]; then
      if (( ch >= 52 && ch <= 64 )) || (( ch >= 100 && ch <= 144 )); then
        echo "  -> Switch router to a non-DFS channel (36, 40, 44, 48, 149, 153, 157, 161)"
        echo "     Current channel $ch is DFS -- radar events can cause 4+ second disruptions."
        recs=$((recs + 1))
      fi
    fi

    # 2.4 GHz band
    local band
    band=$(awk -F, 'NR == 2 { print $22 }' "$main_file")
    if [[ "$band" == "2.4" ]]; then
      echo "  -> Switch to 5 GHz WiFi band"
      echo "     2.4 GHz has only 3 non-overlapping channels and is heavily congested in most homes."
      recs=$((recs + 1))
    fi

    # Weak signal
    if [[ -n "$weak_signal" ]]; then
      echo "  -> Move closer to the router, or add an access point"
      echo "     Signal below -75 dBm causes MCS rate drops and retransmissions."
      recs=$((recs + 1))
    fi

    # High latency spikes
    if [[ -n "$spikes" ]]; then
      echo "  -> Check for background uploads (cloud sync, backups, software updates)"
      echo "     Also check if other devices are streaming or downloading."
      echo "     Consider running 'networkQuality' in Terminal to test for bufferbloat."
      recs=$((recs + 1))
    fi

    # Slow DNS
    if [[ -n "$slow_dns" ]]; then
      echo "  -> Switch to a faster DNS resolver (1.1.1.1 or 8.8.8.8)"
      echo "     Slow DNS adds latency to every new connection during calls."
      recs=$((recs + 1))
    fi
  fi

  if [[ -n "${high_retx:-}" ]]; then
    echo "  -> High retransmits indicate WiFi interference or congestion"
    echo "     Try changing router channel, reducing channel width, or moving closer."
    recs=$((recs + 1))
  fi

  if [[ "$recs" -eq 0 ]]; then
    echo "  No specific recommendations -- network looks healthy."
  fi

  echo
  print_rule
  echo " Raw CSV     : $sample_file"
  [[ "$requested" != "$sample_file" ]] && echo " Input CSV   : $requested"
  [[ -f "$traffic_file" ]] && echo " Traffic CSV : $traffic_file"
  [[ -f "$conn_file" ]] && echo " Connect CSV : $conn_file"
  [[ -f "${main_file%.csv}-udp.csv" ]] && echo " UDP CSV     : ${main_file%.csv}-udp.csv"
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
