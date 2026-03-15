# lib/process.sh — PID file and process management.

[[ -n "${_NETMON_PROCESS_LOADED:-}" ]] && return 0
_NETMON_PROCESS_LOADED=1

[[ -n "${_NETMON_CONFIG_LOADED:-}" ]] || source "$(dirname "${BASH_SOURCE[0]}")/config.sh"

read_pid_file() {
  [[ -f "$PID_FILE" ]] || return 1
  local pid
  pid=$(cat "$PID_FILE" 2>/dev/null || true)
  [[ "$pid" =~ ^[0-9]+$ ]] || return 1
  printf "%s\n" "$pid"
}

pid_is_monitor() {
  local pid="$1"
  kill -0 "$pid" 2>/dev/null || return 1
  local cmdline
  cmdline=$(ps -p "$pid" -o command= 2>/dev/null || true)
  [[ "$cmdline" == *"netmon.sh"* ]]
}

running_monitor_pid() {
  local pid
  pid=$(read_pid_file 2>/dev/null || true)
  [[ -n "$pid" ]] || return 1
  pid_is_monitor "$pid" || return 1
  printf "%s\n" "$pid"
}
