# lib/traffic.sh — nettop snapshots and per-process/connection traffic capture.

[[ -n "${_NETMON_TRAFFIC_LOADED:-}" ]] && return 0
_NETMON_TRAFFIC_LOADED=1

[[ -n "${_NETMON_CONFIG_LOADED:-}" ]] || source "$(dirname "${BASH_SOURCE[0]}")/config.sh"

_nettop_snapshot() {
  # Raw nettop snapshot: process.pid,bytes_in,bytes_out,rx_dupe,rx_ooo,re-tx
  nettop -P -L 1 -n -x -J time,bytes_in,bytes_out,rx_dupe,rx_ooo,re-tx 2>/dev/null |
    awk -F, 'NR > 1 && ($3 + 0 > 0 || $4 + 0 > 0) { print $2 "," $3 "," $4 "," $5 "," $6 "," $7 }'
}

_nettop_udp_snapshot() {
  nettop -m udp -P -L 1 -n -x -J time,bytes_in,bytes_out 2>/dev/null |
    awk -F, 'NR > 1 && ($3 + 0 > 0 || $4 + 0 > 0) { print $2 "," $3 "," $4 }'
}

_nettop_conn_snapshot() {
  nettop -m tcp -L 1 -n -x 2>/dev/null | awk -F, '
    NR == 1 { next }
    $3 == "" && $2 ~ /\.[0-9]+$/ { proc = $2; next }
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

# Shared awk function block for proc/pid extraction from nettop keys.
# Include in awk scripts via: awk "$(cat <<'AWK_FUNCS' ... AWK_FUNCS)" ...
_AWK_PROC_FUNCS='
  function resolve_proc(raw, fullname,   n, p, i, pid, proc) {
    proc = raw; pid = ""
    n = split(raw, p, ".")
    if (n > 1 && p[n] ~ /^[0-9]+$/) {
      pid = p[n]; proc = p[1]
      for (i = 2; i < n; i++) proc = proc "." p[i]
    }
    if (pid != "" && pid in fullname) proc = fullname[pid]
    return proc
  }
  function resolve_pid(raw,   n, p) {
    n = split(raw, p, ".")
    if (n > 1 && p[n] ~ /^[0-9]+$/) return p[n]
    return ""
  }
  function clamp(v) { return (v < 0 ? 0 : v) }
'

capture_traffic() {
  local ts="$1" traffic_file="$2" prev_file="$3" curr_file="$4" name_file="$5"

  if [[ -s "$prev_file" ]]; then
    awk -F, -v ts="$ts" "$_AWK_PROC_FUNCS"'
      FILENAME == ARGV[1] { fullname[$1] = $2; next }
      FILENAME == ARGV[2] { prev[$1] = $2 FS $3 FS $4 FS $5 FS $6; next }
      {
        split(prev[$1], pv, FS)
        din = clamp($2 - pv[1]); dout = clamp($3 - pv[2])
        if (din > 0 || dout > 0) {
          ddup = clamp($4 - pv[3]); dooo = clamp($5 - pv[4]); dretx = clamp($6 - pv[5])
          printf "%s,%s,%s,%d,%d,%d,%d,%d\n", ts, resolve_proc($1, fullname), resolve_pid($1), din, dout, ddup, dooo, dretx
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
    awk -F, -v ts="$ts" "$_AWK_PROC_FUNCS"'
      FILENAME == ARGV[1] { fullname[$1] = $2; next }
      FILENAME == ARGV[2] { prev[$1] = $2 FS $3 FS $4; next }
      {
        split(prev[$1], pv, FS)
        din = clamp($2 - pv[1]); dout = clamp($3 - pv[2]); dretx = clamp($4 - pv[3])
        if (din > 0 || dout > 0) {
          split($1, kp, "|"); proc_raw = kp[1]; flow = kp[2]
          split(flow, lr, "<->")
          remote = (length(lr[2]) > 0 ? lr[2] : flow)
          gsub(/^[[:space:]]+|[[:space:]]+$/, "", remote)
          n = split(remote, rp, ":"); rport = rp[n]; rip = rp[1]
          for (i = 2; i < n; i++) rip = rip ":" rp[i]
          printf "%s,%s,%s,%s,%s,%d,%d,%d\n", ts, resolve_proc(proc_raw, fullname), resolve_pid(proc_raw), rip, rport, din, dout, dretx
        }
      }
    ' "$name_file" "$prev_file" "$curr_file" >>"$conn_file"
  fi

  cp "$curr_file" "$prev_file"
}

capture_udp_traffic() {
  local ts="$1" udp_file="$2" prev_file="$3" curr_file="$4" name_file="$5"

  _nettop_udp_snapshot >"$curr_file"

  if [[ -s "$prev_file" ]]; then
    awk -F, -v ts="$ts" "$_AWK_PROC_FUNCS"'
      FILENAME == ARGV[1] { fullname[$1] = $2; next }
      FILENAME == ARGV[2] { prev[$1] = $2 FS $3; next }
      {
        split(prev[$1], pv, FS)
        din = clamp($2 - pv[1]); dout = clamp($3 - pv[2])
        if (din > 0 || dout > 0) {
          printf "%s,%s,%s,%d,%d\n", ts, resolve_proc($1, fullname), resolve_pid($1), din, dout
        }
      }
    ' "$name_file" "$prev_file" "$curr_file" >>"$udp_file"
  fi

  cp "$curr_file" "$prev_file"
}
