# lib/system.sh — system metrics: CPU, memory, interface errors.

[[ -n "${_NETMON_SYSTEM_LOADED:-}" ]] && return 0
_NETMON_SYSTEM_LOADED=1

[[ -n "${_NETMON_HELPERS_LOADED:-}" ]] || source "$(dirname "${BASH_SOURCE[0]}")/helpers.sh"

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

get_awdl_status() {
  # Check if AWDL (AirDrop/Handoff) interface is active.
  # Returns "active", "inactive", or "unknown".
  local status
  status=$(ifconfig awdl0 2>/dev/null | awk '/status:/ { print $2; exit }')
  echo "${status:-unknown}"
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
