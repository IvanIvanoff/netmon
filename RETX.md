# Retransmit Percentage — How It Works

## What is a retransmit?

A TCP retransmit happens when a sent packet is not acknowledged by the receiver
within a timeout. The TCP stack re-sends the segment. Common causes: WiFi
interference, weak signal, network congestion, or router overload.

## Data source: nettop

macOS `nettop` provides per-process and per-connection network counters.
We request these columns via `-J`:

```
time, packets_in, bytes_in, packets_out, bytes_out, rx_dupe, rx_ooo, re-tx
```

Key fields:

| Field         | Meaning                                      |
|---------------|----------------------------------------------|
| `packets_in`  | Received packets (unique data segments)       |
| `packets_out` | Sent packets (unique data segments, **excluding** retransmissions) |
| `bytes_in`    | Total bytes received                          |
| `bytes_out`   | Total bytes sent                              |
| `re-tx`       | TCP retransmission count (separate from `packets_out`) |
| `rx_dupe`     | Duplicate packets received                    |
| `rx_ooo`      | Out-of-order packets received                 |

**Critical detail:** nettop's `packets_out` does NOT include retransmissions.
Retransmissions are tracked separately in `re-tx`. This means on a bad
connection, `re-tx` can exceed `packets_out` (e.g., if every packet needs
multiple retransmissions).

## Collection pipeline

### Step 1: Raw snapshot

`_nettop_snapshot()` and `_nettop_conn_snapshot()` in `lib/traffic.sh` capture
a single nettop reading. All counters are **cumulative** (total since process
start).

### Step 2: Delta computation

`capture_traffic()` and `capture_connections()` compare the current snapshot
against the previous one and compute deltas using awk:

```
delta_packets_out = clamp(current_packets_out - previous_packets_out)
delta_retx        = clamp(current_retx - previous_retx)
```

`clamp()` ensures deltas are never negative (handles counter resets).

### Step 3: CSV output

Deltas are appended to CSV files each sample cycle (~2 seconds):

**Traffic CSV:**
```
sample_ts, process, pid, bytes_in, bytes_out, packets_in, packets_out, rx_dupe, rx_ooo, retransmits
```

**Connections CSV:**
```
sample_ts, process, pid, remote_ip, remote_port, bytes_in, bytes_out, packets_in, packets_out, retransmits
```

### Step 4: Python accumulation

The TUI (`netmon_tui.py`) sums deltas per process/connection across all samples:

```python
totals[proc][0] += bytes_in       # index 0
totals[proc][1] += bytes_out      # index 1
totals[proc][2] += packets_in     # index 2
totals[proc][3] += packets_out    # index 3
totals[proc][4] += retransmits    # index 4
```

A baseline subtraction removes traffic from before the TUI attached.

## Retransmit percentage formula

```
retx_pct = retransmits / (packets_out + retransmits) × 100
```

The denominator is `packets_out + retransmits` because:
- `packets_out` = unique data segments sent
- `retransmits` = additional re-sent segments
- `packets_out + retransmits` = **total segments on the wire**

This gives a percentage between 0% and 100%:
- **0%** — no retransmissions
- **50%** — half the wire traffic is retransmissions
- **100%** — only retransmissions, no new data (theoretical)

### Why not `retransmits / packets_out`?

That ratio can exceed 100% (e.g., 340 retransmits with 112 unique packets
= 304%). This happens on very bad connections where each packet needs
multiple retransmissions. The `packets_out + retransmits` denominator
keeps the percentage bounded and intuitive.

## Color thresholds

| Retx %    | TUI color | Chart color | Meaning                          |
|-----------|-----------|-------------|----------------------------------|
| 0%        | normal    | grey (-)    | No retransmissions               |
| ≤ 0.5%    | normal    | grey        | Normal, healthy connection       |
| 0.5% – 2% | yellow   | orange      | Elevated — possible interference |
| > 2%      | red       | red         | High — significant packet loss   |

## Display format

In both the TUI and chart, retransmits are shown as:

```
count (pct%)
```

Examples:
- `-` — zero retransmits
- `42 (0.3%)` — 42 retransmits, 0.3% of wire traffic
- `340 (75.2%)` — 340 retransmits, severe loss
- `7000 (1.4%)` — high absolute count but moderate rate

The percentage is more meaningful than the raw count for assessing
connection health, since it accounts for traffic volume.

## Where the formula is used

- **TUI:** `_retx_pct()` in `netmon_tui.py` (TCP processes + connections)
- **Chart Python:** `retx_pct` field in `_aggregate_traffic()`, `_aggregate_connections()`, `_aggregate_by_port()`
- **Chart JS:** `item.retx_pct` rendered in `renderTrafficTable()`

All three use the identical formula: `retx / (packets_out + retx) × 100`.

## VPN processes — why retx% is misleading

VPN processes (NordVPN, ExpressVPN, WireGuard, OpenVPN, Cisco AnyConnect,
Tailscale, etc.) often show extremely high retx percentages (90–99%) that
**do not reflect real packet loss**. This is a measurement artifact.

### Why it happens

VPN daemons create a tunnel interface (e.g., `utun`). Traffic flows like this:

```
App → TCP stack → tunnel interface → VPN process → encrypted UDP/TCP → physical WiFi
```

nettop sees two views of the same traffic:

1. **The app's connections** — normal TCP with accurate `packets_out` and `re-tx`
2. **The VPN process** — handles raw sockets or UDP encapsulation internally

For the VPN process itself, nettop reports:
- **`packets_out`**: very low or zero — the VPN daemon doesn't send data via
  regular TCP sockets; it uses raw sockets, UDP encapsulation, or kernel
  extensions that nettop doesn't count as standard TCP packets
- **`re-tx`**: inherited from inner TCP connections being attributed to the
  VPN process, or from the outer encrypted transport's retransmissions

With `packets_out ≈ 0` and `re-tx > 0`, the formula gives:
```
retx / (0 + retx) × 100 = 100%
```

### What to do

**Ignore retx% for VPN processes.** The per-app connections (Chrome, Zoom, etc.)
still show accurate retx% because their TCP counters are tracked normally —
the VPN tunnel is transparent to them at the TCP level.

If you see high retx% on a non-VPN process while a VPN is active, that IS
a real signal — it means packets are being lost somewhere on the path
(local WiFi, VPN server, or destination).

### Common VPN process names

NordVPN (`nordvpnd`, `NordVPN`), ExpressVPN (`expressvpnd`),
WireGuard (`wireguard-go`), OpenVPN (`openvpn`), Cisco AnyConnect (`vpnagentd`),
Tailscale (`tailscaled`), Mullvad (`mullvad-daemon`), Cloudflare WARP (`warp-svc`),
GlobalProtect (`PanGPS`), macOS built-in (`nesessionmanager`).
