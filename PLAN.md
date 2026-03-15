# netmon Enhancement Plan

This plan adds new WiFi diagnostic capabilities to netmon based on research into common causes of choppy/laggy video calls. Each feature is self-contained and can be implemented independently. Implement them in the order listed — later features may depend on earlier ones.

Read `CLAUDE.md` first for full architecture and command reference.

---

## Background: why these features

The most common root causes of choppy video calls over WiFi, in order of how often people discover them:

1. **Bufferbloat** — latency is fine idle but spikes to 500ms+ when any device uploads/downloads. Video call audio packets queue behind bulk data. The #1 hidden cause.
2. **macOS background WiFi scanning** — `locationd` (Location Services) and AWDL (AirDrop/Handoff) trigger WiFi scans every 30-60s. WiFi firmware can't scan and transfer simultaneously, causing 1-2s latency spikes.
3. **DFS channel radar events** — 5 GHz channels 52-64 and 100-144 share spectrum with weather radar. Radar detection forces immediate channel evacuation, causing 4+ second disruptions.
4. **Band/channel switching mid-call** — switching from 5 GHz to 2.4 GHz (or between channels) causes brief packet loss.
5. **MCS rate drops** — interference bursts cause the radio to fall to lower MCS index, cutting throughput from hundreds of Mbps to single digits. Happens transiently and a simple ping test misses it.
6. **Wide channel instability** — 80/160 MHz channels are 4-8x more vulnerable to interference than 20/40 MHz. Documented M1 Mac bug with 80 MHz WiFi 6.
7. **Channel congestion** — too many networks on the same channel.

Video call platform requirements for reference:

| Metric | Good | Marginal | Bad |
|--------|------|----------|-----|
| Latency (RTT) | < 50ms | 50-150ms | > 150ms |
| Jitter | < 15ms | 15-30ms | > 30ms |
| Packet loss | < 0.5% | 0.5-2% | > 2% |
| RSSI | > -60 dBm | -60 to -70 | < -70 dBm |
| SNR | > 25 dB | 20-25 dB | < 20 dB |

---

## Feature 1: DFS channel warning in diagnostics

**Why:** If the router is on a DFS channel, a radar event can cause 4+ second call disruptions at any moment. Users should be warned so they can change the router channel.

**What to change:**

### `lib/measure.sh`

Add a function `is_dfs_channel()` after the existing `channel_to_band()` function (around line 163):

```bash
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
```

### `netmon_tui.py` — `run_diagnostics()` (line 347)

Add a DFS check after the existing channel band check (after line 437 `if band == "2.4":`):

```python
# -- DFS channel warning --
channel_str = latest.get("channel", "")
ch_num = to_float(channel_str)
if ch_num is not None:
    ch = int(ch_num)
    if (52 <= ch <= 64) or (100 <= ch <= 144):
        issues.append(("warn", f"DFS channel {ch} — radar events can disrupt calls"))
```

### `tests/test_bash_parsers.sh`

Add tests for `is_dfs_channel` after the `channel_to_band` tests:

```bash
echo "=== is_dfs_channel ==="
assert_eq "ch36 -> not DFS" "0" "$(is_dfs_channel 36)"
assert_eq "ch52 -> DFS" "1" "$(is_dfs_channel 52)"
assert_eq "ch100 -> DFS" "1" "$(is_dfs_channel 100)"
assert_eq "ch149 -> not DFS" "0" "$(is_dfs_channel 149)"
```

### `tests/test_diagnostics.py`

Add a test that creates a `latest` dict with `channel: "52"` and verifies `run_diagnostics()` returns a DFS warning.

### No CSV changes needed — uses existing `channel` column.

---

## Feature 2: Band change detection in diagnostics

**Why:** Switching from 5 GHz to 2.4 GHz mid-call causes brief packet loss and dramatically lower throughput. The current diagnostics track channel changes but don't specifically flag band downgrades.

**What to change:**

### `netmon_tui.py` — `parse_main_csv()` (line 156)

Add a new key `band_set` to the result dict (similar to existing `bssid_set` and `channel_set`):

```python
"band_set": set(),
```

In the row loop (around line 200 where `bssid_set` and `channel_set` are populated), add:

```python
band_val = row.get("channel_band", "")
if band_val and band_val != "?":
    result["band_set"].add(band_val)
```

### `netmon_tui.py` — `run_diagnostics()` (line 347)

Add after the channel changes check (after line 445):

```python
# -- Band changes --
band_set: set = main.get("band_set", set())
if "2.4" in band_set and "5" in band_set:
    issues.append(("bad", "Band switch detected: moved between 5 GHz and 2.4 GHz"))
```

### `tests/test_diagnostics.py`

Add a test with fixture data where `channel_band` changes from `"5"` to `"2.4"` and verify the diagnostic fires.

### No CSV changes needed — uses existing `channel_band` column.

---

## Feature 3: Channel width instability diagnostics

**Why:** Wide channels (80/160 MHz) are more vulnerable to interference. Documented M1 Mac bug with 80 MHz WiFi 6. If the user is on a wide channel and experiencing issues, we should suggest narrowing it.

**What to change:**

### `netmon_tui.py` — `run_diagnostics()` (line 347)

Add after the band change detection:

```python
# -- Wide channel + problems --
ch_width = latest.get("channel_width", "")
width_num = to_float(ch_width)
if width_num is not None and width_num >= 80:
    # Only warn if there are also signal/retransmit issues
    has_signal_issues = (rssi_now is not None and rssi_now < -65) or (snr_now is not None and snr_now < 25)
    has_loss = loss_vals and any(v > 0 for v in loss_vals[-10:])
    if has_signal_issues or has_loss:
        issues.append(("warn", f"{int(width_num)} MHz channel width — try 40 MHz for stability"))
```

### No CSV changes needed — uses existing `channel_width` column.

---

## Feature 4: MCS index trend diagnostics

**Why:** MCS rate drops are an early warning of interference — they happen before packet loss appears. A drop from MCS 9 to MCS 2 means throughput collapsed from ~400 Mbps to ~30 Mbps.

**What to change:**

### `netmon_tui.py` — `parse_main_csv()` (line 156)

Add to the result dict initialization:

```python
"mcs_vals": [],
```

In the row parsing loop (where other `*_vals` lists are appended), add:

```python
("mcs_vals", "mcs"),
```

to the existing list of `(key, field)` tuples at line 195.

### `netmon_tui.py` — `run_diagnostics()` (line 347)

Add after the TX rate drops check (after line 432):

```python
# -- MCS index drops --
mcs_vals: List[float] = main.get("mcs_vals", [])
if len(mcs_vals) >= 5:
    recent_mcs = mcs_vals[-5:]
    mcs_min = min(recent_mcs)
    mcs_max = max(mcs_vals)  # all-time max
    if mcs_max - mcs_min >= 4 and mcs_min < 5:
        issues.append(("warn", f"MCS rate drop: {int(mcs_max)} → {int(mcs_min)} (interference)"))
```

### `tests/test_diagnostics.py`

Add a test with MCS values that drop from 9 to 2 and verify the diagnostic fires.

### No CSV changes needed — uses existing `mcs` column.

---

## Feature 5: UDP traffic monitoring via nettop

**Why:** Video calls primarily use UDP, not TCP. Currently netmon only captures TCP traffic and connections. Without UDP, we miss the actual video call traffic.

**What to change:**

### `lib/config.sh`

Add a new CSV header:

```bash
UDP_CSV_HEADER="sample_ts,process,pid,bytes_in,bytes_out"
```

### `lib/traffic.sh`

Add a new function `_nettop_udp_snapshot()` after the existing `_nettop_conn_snapshot()`:

```bash
_nettop_udp_snapshot() {
  # UDP traffic snapshot: process.pid,bytes_in,bytes_out
  nettop -m udp -P -L 1 -n -x -J time,bytes_in,bytes_out 2>/dev/null |
    awk -F, 'NR > 1 && ($3 + 0 > 0 || $4 + 0 > 0) { print $2 "," $3 "," $4 }'
}
```

Add a new function `capture_udp_traffic()` modeled on `capture_traffic()` but simpler (only 3 value columns: bytes_in, bytes_out — no rx_dupe, rx_ooo, retransmits since UDP doesn't have those):

```bash
capture_udp_traffic() {
  local ts="$1" udp_file="$2" prev_file="$3" curr_file="$4" name_file="$5"

  _nettop_udp_snapshot >"$curr_file"

  if [[ -s "$prev_file" ]]; then
    awk -F, -v ts="$ts" '
      FILENAME == ARGV[1] { fullname[$1] = $2; next }
      FILENAME == ARGV[2] { prev_in[$1]=$2; prev_out[$1]=$3; next }
      {
        din = $2 - (prev_in[$1] + 0); if (din < 0) din = 0
        dout = $3 - (prev_out[$1] + 0); if (dout < 0) dout = 0
        if (din > 0 || dout > 0) {
          proc = $1; pid = ""
          n = split(proc, p, ".")
          if (n > 1 && p[n] ~ /^[0-9]+$/) {
            pid = p[n]; proc = p[1]
            for (i = 2; i < n; i++) proc = proc "." p[i]
          }
          if (pid != "" && pid in fullname) proc = fullname[pid]
          printf "%s,%s,%s,%d,%d\n", ts, proc, pid, din, dout
        }
      }
    ' "$name_file" "$prev_file" "$curr_file" >>"$udp_file"
  fi

  cp "$curr_file" "$prev_file"
}
```

### `lib/collector.sh` — `sample_loop()`

1. Add new temp files for UDP snapshots (after `curr_conn` on line 36):
   ```bash
   prev_udp=$(make_tmp_file "uprev")
   curr_udp=$(make_tmp_file "ucurr")
   ```

2. Add the UDP file to the trap cleanup (line 41).

3. Add baseline UDP snapshot after the existing baseline snapshots (after line 45):
   ```bash
   _nettop_udp_snapshot >"$prev_udp" || : >"$prev_udp"
   ```

4. Accept a 5th argument `udp_file` in sample_loop's local declaration (line 18) and write its header:
   ```bash
   echo "$UDP_CSV_HEADER" >"$udp_file"
   ```

5. In the sample loop body, after `capture_connections` (line 101), add:
   ```bash
   capture_udp_traffic "$ts" "$udp_file" "$prev_udp" "$curr_udp" "$name_file" || true
   ```

### `netmon.sh` — `cmd_start()`

Add a UDP file path alongside the existing traffic/connections/scan files:

```bash
udp_file="$LOG_DIR/call-${stamp}-udp.csv"
```

Pass it as the 5th argument to `sample_loop`.

### `netmon_tui.py`

1. In `resolve_related()` (line 132), add the UDP file:
   ```python
   def resolve_related(main_file: Path) -> Tuple[Path, Path, Path, Path]:
       stem = str(main_file)
       base = stem[:-4] if stem.endswith(".csv") else stem
       return (Path(f"{base}-traffic.csv"), Path(f"{base}-connections.csv"),
               Path(f"{base}-scan.csv"), Path(f"{base}-udp.csv"))
   ```

2. Add `parse_udp_totals()` modeled on `parse_traffic_totals()` but with only bytes_in/bytes_out columns (no rx_dupe, rx_ooo, retransmits).

3. Update `draw_dashboard()` to unpack the 4th return value from `resolve_related()` and display UDP traffic in the traffic panel or a new sub-panel.

### Update all callers of `resolve_related()`

`draw_dashboard()` (line 626) currently unpacks 3 values. Update to 4. Search for all call sites.

### Tests

- Add a `tests/fixtures/udp.csv` fixture file.
- Add a `udp_csv` fixture in `tests/conftest.py`.
- Test `parse_udp_totals()` in `tests/test_csv_parsing.py`.
- Update `tests/test_collector_integration.sh` to check the UDP CSV file exists and has the correct header.

---

## Feature 6: `networkQuality` bufferbloat test

**Why:** Bufferbloat is the #1 hidden cause of choppy calls. macOS has a built-in tool (`networkQuality`) that measures responsiveness (RPM) under load. Running it once at session start gives a baseline. Running it periodically (every ~5 min) catches intermittent issues.

**What to change:**

### `lib/config.sh`

Add:

```bash
NETQUALITY_CSV_HEADER="timestamp,dl_throughput,ul_throughput,dl_rpm,ul_rpm,idle_latency_ms,responsiveness"
NETQUALITY_INTERVAL=150  # run networkQuality every N sample cycles (~5 min at 2s interval)
```

### `lib/measure.sh`

Add a function:

```bash
run_network_quality() {
  # Runs Apple's networkQuality tool with JSON output.
  # Returns: dl_throughput|ul_throughput|dl_rpm|ul_rpm|idle_latency|responsiveness
  # Takes ~15 seconds. Run in background.
  local nq_file="$1"
  has_cmd networkQuality || { echo "?" >"$nq_file"; return 0; }

  local json
  json=$(networkQuality -c -s 2>/dev/null || true)
  [[ -n "$json" ]] || { echo "?" >"$nq_file"; return 0; }

  # Parse JSON with python3 (available on all macOS)
  python3 -c "
import json, sys
try:
    d = json.loads('''$json''')
    dl = d.get('dl_throughput', 0)
    ul = d.get('ul_throughput', 0)
    dl_rpm = d.get('dl_responsiveness', 0)
    ul_rpm = d.get('ul_responsiveness', 0)
    idle = d.get('idle_latency_ms', 0)
    resp = d.get('responsiveness', 'unknown')
    print(f'{dl}|{ul}|{dl_rpm}|{ul_rpm}|{idle}|{resp}')
except Exception:
    print('?|?|?|?|?|?')
" >"$nq_file" 2>/dev/null || echo "?|?|?|?|?|?" >"$nq_file"
}
```

**Note:** `networkQuality -c` outputs JSON. The `-s` flag runs sequential (upload then download) instead of parallel, giving cleaner RPM measurements. This takes ~12-15 seconds, so it MUST be run in a background subshell.

**Note:** The JSON parsing uses a heredoc-embedded python3 snippet. Be careful to escape the JSON properly. An alternative is to write the JSON to a temp file and have python3 read from it:

```bash
run_network_quality() {
  local nq_file="$1"
  has_cmd networkQuality || { echo "?" >"$nq_file"; return 0; }

  local json_file
  json_file=$(make_tmp_file "nq_json")
  networkQuality -c -s >"$json_file" 2>/dev/null || { echo "?" >"$nq_file"; rm -f "$json_file"; return 0; }

  python3 - "$json_file" <<'PY' >"$nq_file" 2>/dev/null || echo "?|?|?|?|?|?" >"$nq_file"
import json, sys
try:
    with open(sys.argv[1]) as f:
        d = json.load(f)
    dl = d.get('dl_throughput', 0)
    ul = d.get('ul_throughput', 0)
    dl_rpm = d.get('dl_responsiveness', 0)
    ul_rpm = d.get('ul_responsiveness', 0)
    idle = d.get('idle_latency_ms', 0)
    resp = d.get('responsiveness', 'unknown')
    print(f'{dl}|{ul}|{dl_rpm}|{ul_rpm}|{idle}|{resp}')
except Exception:
    print('?|?|?|?|?|?')
PY
  rm -f "$json_file"
}
```

This second approach (temp file) is safer and avoids shell quoting issues with embedded JSON.

### `lib/collector.sh` — `sample_loop()`

1. Accept a 6th argument `nq_file` (networkQuality CSV file).
2. Write `$NETQUALITY_CSV_HEADER` to `nq_file` at loop start.
3. Add a counter `nq_counter` (similar to `scan_counter`).
4. Also run it on the first sample (cycle 0) to get an immediate baseline.
5. Every `NETQUALITY_INTERVAL` cycles, run `run_network_quality` in the background:

```bash
local nq_result_file
nq_result_file=$(make_tmp_file "nqresult")

# Add to trap cleanup

local nq_counter=0

# Inside the while loop:
nq_counter=$((nq_counter + 1))
if (( nq_counter >= NETQUALITY_INTERVAL )) || [[ "$nq_counter" -eq 1 ]]; then
  nq_counter=0
  run_network_quality "$nq_result_file" &
fi

# After wait for pings, check if nq result is ready:
if [[ -s "$nq_result_file" ]]; then
  local nq_data
  nq_data=$(cat "$nq_result_file" 2>/dev/null || true)
  if [[ "$nq_data" != "?" ]]; then
    IFS="|" read -r nq_dl nq_ul nq_dl_rpm nq_ul_rpm nq_idle nq_resp <<<"$nq_data"
    printf "%s,%s,%s,%s,%s,%s,%s\n" \
      "$(sanitize_csv_field "$ts")" \
      "$(sanitize_csv_field "$nq_dl")" \
      "$(sanitize_csv_field "$nq_ul")" \
      "$(sanitize_csv_field "$nq_dl_rpm")" \
      "$(sanitize_csv_field "$nq_ul_rpm")" \
      "$(sanitize_csv_field "$nq_idle")" \
      "$(sanitize_csv_field "$nq_resp")" >>"$nq_file"
    : >"$nq_result_file"  # clear so we don't re-read it
  fi
fi
```

**Important:** `networkQuality` actively saturates the link during its test (~15 seconds). This WILL cause latency spikes in the main ping measurements during that window. This is expected and actually useful — it shows how the network behaves under load (which is exactly what bufferbloat detection is). However, consider adding a note/marker in diagnostics when a networkQuality test is running.

### `netmon.sh` — `cmd_start()`

Add:

```bash
nq_file="$LOG_DIR/call-${stamp}-netquality.csv"
```

Pass as 6th argument to `sample_loop`.

### `netmon_tui.py`

1. Update `resolve_related()` to return 5 files (add `-netquality.csv`).
2. Add `parse_netquality_csv()` function that reads the CSV and returns the latest RPM values.
3. In `run_diagnostics()`, add bufferbloat checks based on RPM:

```python
# -- Bufferbloat (from networkQuality) --
# RPM < 200 = severe bufferbloat, 200-800 = moderate, > 800 = good
nq_data = main.get("netquality", {})
dl_rpm = nq_data.get("dl_rpm")
ul_rpm = nq_data.get("ul_rpm")
if dl_rpm is not None and dl_rpm < 200:
    issues.append(("bad", f"Bufferbloat detected: download RPM {dl_rpm} (Low)"))
elif dl_rpm is not None and dl_rpm < 800:
    issues.append(("warn", f"Moderate bufferbloat: download RPM {dl_rpm}"))
if ul_rpm is not None and ul_rpm < 200:
    issues.append(("bad", f"Upload bufferbloat: upload RPM {ul_rpm} (Low)"))
```

4. Display RPM and responsiveness grade in the dashboard (in the health/session panel).

### Tests

- Add `tests/fixtures/netquality.csv` with sample data.
- Test `parse_netquality_csv()` in Python tests.
- Test the RPM diagnostic thresholds.
- Update integration test to check the netquality CSV file exists.

---

## Feature 7: AWDL interface status check

**Why:** AWDL (Apple Wireless Direct Link) is used by AirDrop and Handoff. It periodically scans for nearby Apple devices, causing intermittent lag spikes during calls.

**What to change:**

### `lib/system.sh`

Add a function:

```bash
get_awdl_status() {
  # Check if AWDL interface is active. Returns "up" or "down".
  local status
  status=$(ifconfig awdl0 2>/dev/null | awk '/status:/ { print $2; exit }')
  echo "${status:-unknown}"
}
```

### `lib/collector.sh` — `sample_loop()`

Collect AWDL status. This requires adding a new column to the main CSV.

### `lib/config.sh`

Update `MAIN_CSV_HEADER` to append `,awdl_status`:

```
MAIN_CSV_HEADER="timestamp,ssid,channel,...,cpu_usage,mem_pressure,awdl_status"
```

**This changes the column count from 27 to 28.** Update all places that reference the column count:
- `tests/test_collector_integration.sh` line 162: change `"27"` to `"28"`
- `tests/test_collector_integration.sh` line 179: change `"27"` to `"28"` (in the assertion message)
- All test fixture CSV files in `tests/fixtures/` need the new column added.

### `lib/collector.sh`

In the sample loop, collect AWDL status and append it to the CSV row:

```bash
local awdl_status
awdl_status=$(get_awdl_status)
```

Add `"$(sanitize_csv_field "$awdl_status")"` to the end of the printf format string.

### `netmon_tui.py` — `run_diagnostics()`

```python
# -- AWDL active --
awdl = latest.get("awdl_status", "")
if awdl == "active":
    issues.append(("warn", "AWDL active (AirDrop/Handoff) — may cause periodic lag"))
```

### Tests

- Update all fixture CSVs to add the new column.
- Update `conftest.py` header-only fixture.
- Add bash test for `get_awdl_status` (mock output).
- Add Python diagnostic test for AWDL warning.

---

## Feature 8: `wdutil info` integration for CCA% (channel utilization)

**Why:** CCA (Clear Channel Assessment) percentage tells you how busy the WiFi channel is. High CCA means congestion even if your signal is strong. `wdutil` is Apple's replacement for the deprecated `airport` tool and provides CCA% plus other metrics not available from `airport`.

**What to change:**

### `lib/wifi.sh`

Add a function to parse `wdutil info` output. Note: `wdutil info` requires `sudo` on some macOS versions. Test without sudo first:

```bash
get_wdutil_info() {
  # Parse wdutil info for CCA%, PHY mode, guard interval.
  # Returns: cca_pct|phy_mode|guard_interval
  # Falls back to ?|?|? if wdutil is unavailable or fails.
  local output
  output=$(wdutil info 2>/dev/null || true)
  [[ -n "$output" ]] || { echo "?|?|?"; return 0; }

  local cca phy gi
  cca=$(echo "$output" | awk '/CCA/ { for(i=1;i<=NF;i++) if($i ~ /^[0-9]+%?$/) { sub(/%/,"",$i); print $i; exit } }')
  phy=$(echo "$output" | awk '/PHY Mode/ { for(i=NF;i>=1;i--) if($i !~ /^PHY$|^Mode:?$/) { print $i; exit } }')
  gi=$(echo "$output" | awk '/Guard Interval/ { print $NF; exit }')

  echo "${cca:-?}|${phy:-?}|${gi:-?}"
}
```

**Important:** `wdutil info` output format varies between macOS versions. Test the parsing on the actual machine before committing. The output looks roughly like:

```
WIFI INTERFACE (en0)
  ...
  CCA                        : 12%
  PHY Mode                   : 802.11ax
  Guard Interval             : 800ns
  ...
```

Parse conservatively — if a field can't be found, output `?`.

### `lib/config.sh`

Add `cca_pct` to `MAIN_CSV_HEADER` (column 29). This is a second column-count bump (after AWDL in feature 7, so column count goes from 28 to 29).

### `lib/collector.sh`

Collect CCA in the sample loop:

```bash
local wdutil_data cca_pct
wdutil_data=$(get_wdutil_info || echo "?|?|?")
IFS="|" read -r cca_pct _phy_mode _guard_interval <<<"$wdutil_data"
cca_pct="${cca_pct:-?}"
```

Append to CSV row.

### `netmon_tui.py`

1. Parse `cca_pct` in `parse_main_csv()` — add to `cca_vals` list.
2. Add to `run_diagnostics()`:

```python
# -- Channel utilization --
cca_vals: List[float] = main.get("cca_vals", [])
if cca_vals:
    recent_cca = cca_vals[-5:]
    cca_avg = sum(recent_cca) / len(recent_cca)
    if cca_avg > 70:
        issues.append(("bad", f"High channel utilization: {cca_avg:.0f}% (congested)"))
    elif cca_avg > 40:
        issues.append(("warn", f"Moderate channel utilization: {cca_avg:.0f}%"))
```

3. Display CCA% in the WiFi details panel of the dashboard.
4. Add `value_attr()` thresholds for CCA (good < 30, warn 30-60, bad > 60).

### Tests

- Update all fixture CSVs for new column.
- Add bash test for `get_wdutil_info` parsing with canned output.
- Add Python diagnostic test for CCA thresholds.
- Update integration test column count.

---

## Feature 9: Post-session recommendations in `cmd_review`

**Why:** After collecting all this data, the report should tell the user what to do about the problems found — not just list them.

**What to change:**

### `lib/report.sh` — `cmd_review()`

After the "Issues Detected" section (around the end of cmd_review), add a "Recommendations" section that maps detected issues to specific fixes:

```bash
_section "Recommendations"
local recs=0

# DFS channel
if [[ "$main_available" -eq 1 ]]; then
  local ch
  ch=$(awk -F, 'NR == 2 { print $3 }' "$main_file")
  if [[ "$ch" =~ ^[0-9]+$ ]]; then
    if (( ch >= 52 && ch <= 64 )) || (( ch >= 100 && ch <= 144 )); then
      echo "  → Switch router to a non-DFS channel (36, 40, 44, 48, 149, 153, 157, 161)"
      echo "    Current channel $ch is DFS — radar events can cause 4+ second disruptions."
      recs=$((recs + 1))
    fi
  fi
fi

# Bufferbloat (if networkQuality CSV available)
local nq_csv="${main_file%.csv}-netquality.csv"
if [[ -f "$nq_csv" ]] && [[ $(wc -l <"$nq_csv") -gt 1 ]]; then
  local low_rpm
  low_rpm=$(awk -F, 'NR > 1 && $4 + 0 < 200 { found=1 } END { print found+0 }' "$nq_csv")
  if [[ "$low_rpm" -eq 1 ]]; then
    echo "  → Enable SQM (Smart Queue Management) on your router"
    echo "    Bufferbloat detected — latency spikes under load. Use fq_codel or CAKE algorithm."
    echo "    Test: run 'networkQuality' in Terminal to verify."
    recs=$((recs + 1))
  fi
fi

# 2.4 GHz band
if [[ "$main_available" -eq 1 ]]; then
  local band
  band=$(awk -F, 'NR == 2 { print $22 }' "$main_file")
  if [[ "$band" == "2.4" ]]; then
    echo "  → Switch to 5 GHz WiFi band"
    echo "    2.4 GHz has only 3 non-overlapping channels and is heavily congested in most homes."
    recs=$((recs + 1))
  fi
fi

# Weak signal
if [[ -n "$weak_signal" ]]; then
  echo "  → Move closer to the router, or add an access point"
  echo "    Signal below -75 dBm causes MCS rate drops and retransmissions."
  recs=$((recs + 1))
fi

# High latency spikes
if [[ -n "$spikes" ]]; then
  echo "  → Check for background uploads (cloud sync, backups, software updates)"
  echo "    Also check if other devices are streaming or downloading."
  recs=$((recs + 1))
fi

if [[ "$recs" -eq 0 ]]; then
  echo "  No specific recommendations — network looks healthy."
fi
echo
```

### No tests needed for this specifically — it's display-only formatting in the report. But verify manually with `./netmon.sh review` on a session log.

---

## Implementation order

1. **Feature 1** (DFS warning) — trivial, no CSV changes, immediate value
2. **Feature 2** (Band change detection) — trivial, no CSV changes
3. **Feature 3** (Channel width diagnostics) — trivial, no CSV changes
4. **Feature 4** (MCS trend diagnostics) — small, no CSV changes
5. **Feature 9** (Post-session recommendations) — standalone, no CSV changes, big UX win
6. **Feature 5** (UDP traffic) — medium effort, new CSV file, new functions
7. **Feature 6** (networkQuality bufferbloat) — medium effort, new CSV file, background process
8. **Feature 7** (AWDL status) — small but changes main CSV column count (breaks fixtures)
9. **Feature 8** (wdutil CCA%) — small but also changes column count, depends on testing wdutil output format

Features 1-5 and 9 can be done without changing any CSV headers or fixture files. Do them first. Features 6-8 require CSV/fixture changes and should be batched together.

---

## Validation checklist

After implementing each feature, run all three test suites:

```bash
uv run pytest tests/               # Python tests (should stay at 182+ passing)
bash tests/test_bash_parsers.sh     # Bash parser tests (should stay at 46+ passing)
bash tests/test_collector_integration.sh  # Integration test (~20s)
```

After implementing features that change the main CSV header (7, 8), also:
- Update all files in `tests/fixtures/` to add the new columns
- Update `tests/conftest.py` `header_only_csv` fixture
- Update the column count assertions in `tests/test_collector_integration.sh`
- Update `CLAUDE.md` to reflect the new column count
