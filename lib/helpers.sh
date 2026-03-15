# lib/helpers.sh — generic utility functions.

[[ -n "${_NETMON_HELPERS_LOADED:-}" ]] && return 0
_NETMON_HELPERS_LOADED=1

[[ -n "${_NETMON_CONFIG_LOADED:-}" ]] || source "$(dirname "${BASH_SOURCE[0]}")/config.sh"

timestamp() { date "+%Y-%m-%d %H:%M:%S"; }

has_cmd() { command -v "$1" >/dev/null 2>&1; }

warn() { printf "Warning: %s\n" "$*" >&2; }

die() {
  printf "Error: %s\n" "$*" >&2
  exit 1
}

ensure_log_dir() { mkdir -p "$LOG_DIR"; }

assert_supported_os() {
  [[ "$(uname -s)" == "Darwin" ]] || die "netmon.sh currently supports macOS only."
}

validate_interval() {
  [[ "$INTERVAL" =~ ^[0-9]+([.][0-9]+)?$ ]] || die "MONITOR_INTERVAL must be a positive number."
}

repeat_char() {
  local char="$1" count="$2" i
  ((count > 0)) || return 0
  for ((i = 0; i < count; i++)); do
    printf "%s" "$char"
  done
}

print_rule() {
  repeat_char "=" "$REPORT_WIDTH"
  echo
}

_section() {
  local label="-- $1 "
  local pad=$((REPORT_WIDTH - ${#label}))
  printf "%s" "$label"
  repeat_char "-" "$pad"
  echo
}

sanitize_csv_field() {
  local value="${1:-}"
  value=${value//$'\n'/ }
  value=${value//$'\r'/ }
  value=${value//,/;}
  printf "%s" "$value"
}

make_tmp_file() {
  local suffix="$1" tmp
  tmp=$(mktemp -t "netmon.${suffix}.XXXXXX" 2>/dev/null) || tmp=""
  if [[ -z "$tmp" ]]; then
    tmp="/tmp/netmon_${suffix}_$$.$RANDOM"
    : >"$tmp"
  fi
  printf "%s\n" "$tmp"
}

latest_main_log() {
  local latest="" file
  shopt -s nullglob
  for file in "$LOG_DIR"/call-*.csv; do
    [[ "$file" == *-traffic.csv ]] && continue
    [[ "$file" == *-connections.csv ]] && continue
    [[ "$file" == *-scan.csv ]] && continue
    [[ "$file" == *-udp.csv ]] && continue
    [[ "$file" == *-diagnostics.csv ]] && continue
    [[ -z "$latest" || "$file" -nt "$latest" ]] && latest="$file"
  done
  shopt -u nullglob
  printf "%s\n" "$latest"
}
