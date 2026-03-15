# lib/measure.sh — active network measurements: ping, DNS, gateway, jitter.

[[ -n "${_NETMON_MEASURE_LOADED:-}" ]] && return 0
_NETMON_MEASURE_LOADED=1

[[ -n "${_NETMON_CONFIG_LOADED:-}" ]] || source "$(dirname "${BASH_SOURCE[0]}")/config.sh"
[[ -n "${_NETMON_HELPERS_LOADED:-}" ]] || source "$(dirname "${BASH_SOURCE[0]}")/helpers.sh"

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

is_dfs_channel() {
  local ch="$1"
  [[ "$ch" =~ ^[0-9]+$ ]] || { echo "0"; return 0; }
  # DFS channels in the US: 52-64 and 100-144
  if (( ch >= 52 && ch <= 64 )) || (( ch >= 100 && ch <= 144 )); then
    echo "1"
  else
    echo "0"
  fi
}
