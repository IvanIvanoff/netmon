# lib/traffic.sh — nettop snapshots and per-process/connection traffic capture.

[[ -n "${_NETMON_TRAFFIC_LOADED:-}" ]] && return 0
_NETMON_TRAFFIC_LOADED=1

[[ -n "${_NETMON_CONFIG_LOADED:-}" ]] || source "$(dirname "${BASH_SOURCE[0]}")/config.sh"

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
