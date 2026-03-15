# lib/wifi.sh — WiFi info gathering, parsing, and environment scanning.

[[ -n "${_NETMON_WIFI_LOADED:-}" ]] && return 0
_NETMON_WIFI_LOADED=1

[[ -n "${_NETMON_CONFIG_LOADED:-}" ]] || source "$(dirname "${BASH_SOURCE[0]}")/config.sh"
[[ -n "${_NETMON_HELPERS_LOADED:-}" ]] || source "$(dirname "${BASH_SOURCE[0]}")/helpers.sh"

maybe_compile_wifi_helper() {
  [[ -x "$WIFI_HELPER" ]] && return 0

  if [[ -x "$AIRPORT_PATH" ]] && "$AIRPORT_PATH" -I 2>/dev/null | grep -q "agrCtlRSSI"; then
    return 0
  fi

  if ! has_cmd swiftc; then
    warn "swiftc is not available; Wi-Fi RSSI/noise metrics may be unavailable."
    return 0
  fi

  local tmp_binary
  tmp_binary=$(make_tmp_file "wifi_helper")

  if swiftc -O -o "$tmp_binary" - 2>&1 | head -5 <<'SWIFT'
import CoreWLAN
guard let iface = CWWiFiClient.shared().interface() else { exit(1) }
print("     agrCtlRSSI: \(iface.rssiValue())")
print("     agrCtlNoise: \(iface.noiseMeasurement())")
let ssid = iface.ssid() ?? "unknown"
print("          SSID: \(ssid)")
let ch = iface.wlanChannel()
let chNum = ch?.channelNumber ?? 0
print("       channel: \(chNum)")
print("     lastTxRate: \(Int(iface.transmitRate()))")
let bssid = iface.bssid() ?? "?"
print("         BSSID: \(bssid)")
var chWidth = "?"
if let cw = ch {
    switch cw.channelWidth {
    case .width20MHz: chWidth = "20"
    case .width40MHz: chWidth = "40"
    case .width80MHz: chWidth = "80"
    case .width160MHz: chWidth = "160"
    @unknown default: chWidth = "?"
    }
}
print("  channelWidth: \(chWidth)")
SWIFT
  then
    mv "$tmp_binary" "$WIFI_HELPER"
    chmod +x "$WIFI_HELPER"
  else
    rm -f "$tmp_binary"
    warn "Failed to compile CoreWLAN helper; continuing without it."
  fi
}

get_wifi_info() {
  local out

  if [[ -x "$AIRPORT_PATH" ]]; then
    out=$("$AIRPORT_PATH" -I 2>/dev/null || true)
    if grep -q "agrCtlRSSI" <<<"$out"; then
      printf "%s\n" "$out"
      return 0
    fi
  fi

  if [[ -x "$WIFI_HELPER" ]]; then
    out=$("$WIFI_HELPER" 2>/dev/null || true)
    if grep -q "agrCtlRSSI" <<<"$out"; then
      printf "%s\n" "$out"
      return 0
    fi
  fi

  printf "%s\n" "wifi: unavailable"
}

parse_wifi_info() {
  awk '
    BEGIN {
      ssid = "unknown"; channel = "?"; rssi = "?"; noise = "?"
      tx_rate = "?"; bssid = "?"; mcs_idx = "?"; ch_width = "?"
    }
    $1 == "SSID:" {
      $1 = ""
      sub(/^ +/, "", $0)
      if ($0 != "") ssid = $0
    }
    $1 == "channel:" {
      raw = $2
      channel = raw
      sub(/,.*/, "", channel)
      if (raw ~ /,/) {
        cw = raw; sub(/[^,]*,/, "", cw)
        if (cw ~ /^[0-9]+$/) ch_width = cw
      }
    }
    $1 == "agrCtlRSSI:" { rssi = $2 }
    $1 == "agrCtlNoise:" { noise = $2 }
    $1 == "lastTxRate:" { tx_rate = $2 }
    $1 == "maxRate:" { tx_rate = $2 }
    $1 == "BSSID:" { bssid = $2 }
    $1 == "MCS:" { mcs_idx = $2 }
    $1 == "channelWidth:" { ch_width = $2 }
    END {
      printf "%s|%s|%s|%s|%s|%s|%s|%s\n", ssid, channel, rssi, noise, tx_rate, bssid, mcs_idx, ch_width
    }
  '
}

fallback_ssid() {
  has_cmd networksetup || return 0
  local line
  line=$(networksetup -getairportnetwork en0 2>/dev/null || true)
  case "$line" in
  *": "*)
    printf "%s\n" "${line#*: }"
    ;;
  *)
    return 0
    ;;
  esac
}

run_wifi_scan() {
  # Uses system_profiler to get nearby networks + extended current network info.
  # Also writes PHY mode and MCS to a sidecar file for the sample loop.
  local scan_file="$1" ts="$2" ext_file="$3"
  local sp_output
  sp_output=$(system_profiler SPAirPortDataType 2>/dev/null || true)
  [[ -n "$sp_output" ]] || return 0

  # Extract current network PHY mode and MCS into sidecar file
  printf "%s\n" "$sp_output" | awk '
    /Current Network Information:/,/Other Local Wi-Fi Networks:/ {
      if (/PHY Mode:/) { sub(/.*PHY Mode: */, ""); print "phy_mode=" $0 }
      if (/MCS Index:/) { sub(/.*MCS Index: */, ""); print "mcs_index=" $0 }
    }
  ' >"$ext_file"

  # Extract neighboring networks into scan CSV
  printf "%s\n" "$sp_output" | awk -v ts="$ts" '
    /Other Local Wi-Fi Networks:/,0 {
      if (/Other Local Wi-Fi Networks:/) { next }
      if (/:$/) {
        if (ssid != "" && ch != "") {
          gsub(/,/, ";", ssid)
          gsub(/,/, ";", ch)
          gsub(/,/, ";", sec)
          printf "%s,%s,%s,%s,%s,%s\n", ts, ssid, "-", rssi, ch, sec
        }
        ssid = $0; gsub(/^ *| *:$/, "", ssid)
        ch = ""; rssi = "?"; sec = ""
        next
      }
      if (/Channel:/) {
        sub(/.*Channel: */, "")
        ch = $0
      }
      if (/Security:/) { sub(/.*Security: */, ""); sec = $0 }
      if (/Signal \/ Noise:/) {
        sub(/.*Signal \/ Noise: */, "")
        sub(/ dBm.*/, "")
        rssi = $0
      }
    }
    END {
      if (ssid != "" && ch != "") {
        gsub(/,/, ";", ssid)
        gsub(/,/, ";", ch)
        gsub(/,/, ";", sec)
        printf "%s,%s,%s,%s,%s,%s\n", ts, ssid, "-", rssi, ch, sec
      }
    }
  ' >>"$scan_file"
}

get_cca_percent() {
  # Get CCA (Clear Channel Assessment) percentage from wdutil info.
  # Requires sudo; uses sudo -n (non-interactive) to avoid password prompts.
  # Returns a number (e.g. "12") or "?" if unavailable.
  local output
  output=$(sudo -n wdutil info 2>/dev/null || true)
  if [[ -n "$output" ]]; then
    local cca
    cca=$(echo "$output" | awk '/CCA/ { for(i=1;i<=NF;i++) if($i ~ /^[0-9]+%?$/) { sub(/%/,"",$i); print $i; exit } }')
    echo "${cca:-?}"
  else
    echo "?"
  fi
}
