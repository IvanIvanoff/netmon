#!/usr/bin/env python3
"""
netmon_tui.py

Live ncurses dashboard for netmon CSV logs.
Reads existing CSV files and refreshes statistics in-place.
Extended with WiFi details, gateway/jitter, system health,
WiFi environment scan, sparklines, and live diagnostics.
"""

from __future__ import annotations

import argparse
import csv
import curses
import os
import signal
import sys
import time
from collections import defaultdict
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Tuple


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Live TUI for netmon logs")
    parser.add_argument("--main-file", default="", help="Main call-*.csv file to monitor")
    parser.add_argument("--log-dir", default=str(Path.home() / "call-network-logs"))
    parser.add_argument("--pid-file", default="", help="Optional PID file for collector status")
    parser.add_argument("--refresh", type=float, default=1.0, help="Refresh interval in seconds")
    return parser.parse_args()


# ---------------------------------------------------------------------------
# Numeric helpers
# ---------------------------------------------------------------------------

def to_float(value: str) -> Optional[float]:
    if value is None:
        return None
    value = value.strip()
    if not value or value == "?":
        return None
    try:
        return float(value)
    except ValueError:
        return None


def to_int(value: str) -> int:
    parsed = to_float(value)
    if parsed is None:
        return 0
    return int(parsed)


def avg(values: Iterable[float]) -> Optional[float]:
    seq = list(values)
    if not seq:
        return None
    return sum(seq) / len(seq)


def human_bytes(num: int) -> str:
    num = max(0, int(num))
    if num >= 1024**3:
        return f"{num / 1024**3:.1f} GB"
    if num >= 1024**2:
        return f"{num / 1024**2:.1f} MB"
    if num >= 1024:
        return f"{num / 1024:.1f} KB"
    return f"{num} B"


def fmt_num(v: Optional[float], suffix: str = "") -> str:
    if v is None:
        return "n/a"
    if abs(v) >= 100:
        return f"{v:.0f}{suffix}"
    return f"{v:.1f}{suffix}"


def na_style(theme: Dict[str, int], value: str, normal_style: int) -> int:
    """Return dim style if value is 'n/a', otherwise the given style."""
    return theme["dim"] if value == "n/a" else normal_style


def calc_duration(first_ts: str, last_ts: str) -> str:
    """Calculate human-readable duration between two timestamps."""
    if first_ts == "n/a" or last_ts == "n/a":
        return "n/a"
    try:
        from datetime import datetime
        fmt = "%Y-%m-%d %H:%M:%S"
        t0 = datetime.strptime(first_ts, fmt)
        t1 = datetime.strptime(last_ts, fmt)
        delta = int((t1 - t0).total_seconds())
        if delta < 0:
            return "n/a"
        hours, remainder = divmod(delta, 3600)
        minutes, seconds = divmod(remainder, 60)
        if hours > 0:
            return f"{hours}h {minutes:02d}m {seconds:02d}s"
        if minutes > 0:
            return f"{minutes}m {seconds:02d}s"
        return f"{seconds}s"
    except Exception:
        return "n/a"


# ---------------------------------------------------------------------------
# File discovery
# ---------------------------------------------------------------------------

def latest_main_log(log_dir: Path) -> Optional[Path]:
    # New format: call-STAMP/main.csv
    candidates = list(log_dir.glob("call-*/main.csv"))
    # Old format: call-STAMP.csv (flat files)
    for path in log_dir.glob("call-*.csv"):
        name = path.name
        if name.endswith(("-traffic.csv", "-connections.csv", "-scan.csv",
                          "-udp.csv", "-diagnostics.csv")):
            continue
        candidates.append(path)
    if not candidates:
        return None
    candidates.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    return candidates[0]


def _is_session_dir(main_file: Path) -> bool:
    """True if main_file lives in a per-session directory (new format)."""
    return main_file.name == "main.csv"


def resolve_related(main_file: Path) -> Tuple[Path, Path, Path, Path, Path]:
    if _is_session_dir(main_file):
        d = main_file.parent
        return (d / "traffic.csv", d / "connections.csv",
                d / "scan.csv", d / "udp.csv",
                d / "diagnostics.csv")
    # Old flat format: call-STAMP.csv → call-STAMP-traffic.csv etc.
    stem = str(main_file)
    base = stem[:-4] if stem.endswith(".csv") else stem
    return (Path(f"{base}-traffic.csv"), Path(f"{base}-connections.csv"),
            Path(f"{base}-scan.csv"), Path(f"{base}-udp.csv"),
            Path(f"{base}-diagnostics.csv"))


def collector_status(pid_file: Optional[Path]) -> Tuple[bool, Optional[int]]:
    if not pid_file or not pid_file.exists():
        return False, None
    try:
        pid = int(pid_file.read_text().strip())
    except (OSError, ValueError):
        return False, None
    try:
        os.kill(pid, 0)
    except OSError:
        return False, pid
    return True, pid


# ---------------------------------------------------------------------------
# CSV parsing
# ---------------------------------------------------------------------------

def parse_main_csv(path: Path) -> Dict[str, object]:
    result: Dict[str, object] = {
        "samples": 0,
        "first_ts": "n/a",
        "last_ts": "n/a",
        "ping_target": "n/a",
        "latest": {},
        "ping_vals": [],
        "loss_vals": [],
        "rssi_vals": [],
        "snr_vals": [],
        "tx_vals": [],
        "dns_vals": [],
        "gw_ping_vals": [],
        "jitter_vals": [],
        "cpu_vals": [],
        "mem_vals": [],
        "if_ierrs_vals": [],
        "if_oerrs_vals": [],
        "mcs_vals": [],
        "cca_vals": [],
        "bssid_set": set(),
        "channel_set": set(),
        "band_set": set(),
    }

    if not path.exists():
        return result

    try:
        with path.open(newline="") as f:
            reader = csv.DictReader(f)
            for row in reader:
                ts = row.get("timestamp", "")
                result["samples"] = int(result["samples"]) + 1
                if result["first_ts"] == "n/a":
                    result["first_ts"] = ts or "n/a"
                result["last_ts"] = ts or result["last_ts"]
                result["latest"] = row
                if row.get("ping_target"):
                    result["ping_target"] = row.get("ping_target")

                for key, field in [
                    ("ping_vals", "ping_avg_ms"),
                    ("loss_vals", "loss_%"),
                    ("rssi_vals", "rssi_dBm"),
                    ("snr_vals", "snr_dB"),
                    ("tx_vals", "tx_rate_Mbps"),
                    ("dns_vals", "dns_ms"),
                    ("gw_ping_vals", "gw_ping_ms"),
                    ("jitter_vals", "jitter_ms"),
                    ("cpu_vals", "cpu_usage"),
                    ("mem_vals", "mem_pressure"),
                    ("if_ierrs_vals", "if_ierrs"),
                    ("if_oerrs_vals", "if_oerrs"),
                    ("mcs_vals", "mcs"),
                    ("cca_vals", "cca_pct"),
                ]:
                    v = to_float(row.get(field, ""))
                    if v is not None:
                        result[key].append(v)

                bssid = row.get("bssid", "")
                if bssid and bssid != "?":
                    result["bssid_set"].add(bssid)
                ch = row.get("channel", "")
                if ch and ch != "?":
                    result["channel_set"].add(ch)
                band_val = row.get("channel_band", "")
                if band_val and band_val != "?":
                    result["band_set"].add(band_val)
    except Exception:
        return result

    return result


def parse_traffic_totals(path: Path) -> Dict[str, List[int]]:
    if not path.exists():
        return {}
    totals: Dict[str, List[int]] = defaultdict(lambda: [0, 0, 0])
    try:
        with path.open(newline="") as f:
            reader = csv.DictReader(f)
            for row in reader:
                proc = row.get("process", "") or "unknown"
                totals[proc][0] += to_int(row.get("bytes_in", "0"))
                totals[proc][1] += to_int(row.get("bytes_out", "0"))
                totals[proc][2] += to_int(row.get("retransmits", "0"))
    except Exception:
        return {}
    return totals


def top_traffic_rows(totals: Dict[str, List[int]]) -> List[Tuple[str, int, int, int]]:
    items = [(proc, vals[0], vals[1], vals[2]) for proc, vals in totals.items()]
    items.sort(key=lambda x: x[1] + x[2], reverse=True)
    return items[:10]


def parse_connection_totals(path: Path) -> Dict[Tuple[str, str], List[int]]:
    if not path.exists():
        return {}
    totals: Dict[Tuple[str, str], List[int]] = defaultdict(lambda: [0, 0, 0])
    try:
        with path.open(newline="") as f:
            reader = csv.DictReader(f)
            for row in reader:
                proc = row.get("process", "") or "unknown"
                remote = row.get("remote_ip", "") or "unknown"
                key = (proc, remote)
                totals[key][0] += to_int(row.get("bytes_in", "0"))
                totals[key][1] += to_int(row.get("bytes_out", "0"))
                totals[key][2] += to_int(row.get("retransmits", "0"))
    except Exception:
        return {}
    return totals


def top_connection_rows(totals: Dict[Tuple[str, str], List[int]]) -> List[Tuple[str, str, int, int, int]]:
    items = [(proc, remote, vals[0], vals[1], vals[2]) for (proc, remote), vals in totals.items()]
    items.sort(key=lambda x: x[2] + x[3], reverse=True)
    return items[:10]


def parse_udp_totals(path: Path) -> Dict[str, List[int]]:
    if not path.exists():
        return {}
    totals: Dict[str, List[int]] = defaultdict(lambda: [0, 0])
    try:
        with path.open(newline="") as f:
            reader = csv.DictReader(f)
            for row in reader:
                proc = row.get("process", "") or "unknown"
                totals[proc][0] += to_int(row.get("bytes_in", "0"))
                totals[proc][1] += to_int(row.get("bytes_out", "0"))
    except Exception:
        return {}
    return totals


def top_udp_rows(totals: Dict[str, List[int]]) -> List[Tuple[str, int, int]]:
    items = [(proc, vals[0], vals[1]) for proc, vals in totals.items()]
    items.sort(key=lambda x: x[1] + x[2], reverse=True)
    return items[:10]


def subtract_udp_totals(
    current: Dict[str, List[int]], baseline: Dict[str, List[int]]
) -> Dict[str, List[int]]:
    out: Dict[str, List[int]] = {}
    for proc, curr_vals in current.items():
        base_vals = baseline.get(proc, [0, 0])
        diff = [max(0, c - b) for c, b in zip(curr_vals, base_vals)]
        if diff[0] > 0 or diff[1] > 0:
            out[proc] = diff
    return out


def parse_scan_csv(path: Path) -> List[Dict[str, str]]:
    """Parse wifi scan CSV, return latest scan's rows grouped by scan_ts."""
    if not path.exists():
        return []
    rows: List[Dict[str, str]] = []
    last_ts = ""
    try:
        with path.open(newline="") as f:
            reader = csv.DictReader(f)
            for row in reader:
                ts = row.get("scan_ts", "")
                if ts != last_ts:
                    last_ts = ts
                    rows = []  # reset to latest scan
                rows.append(row)
    except Exception:
        return []
    return rows


def subtract_traffic_totals(
    current: Dict[str, List[int]], baseline: Dict[str, List[int]]
) -> Dict[str, List[int]]:
    out: Dict[str, List[int]] = {}
    for proc, curr_vals in current.items():
        base_vals = baseline.get(proc, [0, 0, 0])
        recv = max(0, curr_vals[0] - base_vals[0])
        sent = max(0, curr_vals[1] - base_vals[1])
        retx = max(0, curr_vals[2] - base_vals[2])
        if recv > 0 or sent > 0 or retx > 0:
            out[proc] = [recv, sent, retx]
    return out


def subtract_connection_totals(
    current: Dict[Tuple[str, str], List[int]], baseline: Dict[Tuple[str, str], List[int]]
) -> Dict[Tuple[str, str], List[int]]:
    out: Dict[Tuple[str, str], List[int]] = {}
    for key, curr_vals in current.items():
        base_vals = baseline.get(key, [0, 0, 0])
        recv = max(0, curr_vals[0] - base_vals[0])
        sent = max(0, curr_vals[1] - base_vals[1])
        retx = max(0, curr_vals[2] - base_vals[2])
        if recv > 0 or sent > 0 or retx > 0:
            out[key] = [recv, sent, retx]
    return out


# ---------------------------------------------------------------------------
# Sparkline
# ---------------------------------------------------------------------------

SPARK_CHARS = " ▁▂▃▄▅▆▇█"


def sparkline(values: List[float], width: int = 20) -> str:
    if not values:
        return ""
    tail = values[-width:]
    lo = min(tail)
    hi = max(tail)
    rng = hi - lo if hi != lo else 1.0
    result = []
    for v in tail:
        idx = int((v - lo) / rng * (len(SPARK_CHARS) - 1))
        idx = max(0, min(idx, len(SPARK_CHARS) - 1))
        result.append(SPARK_CHARS[idx])
    return "".join(result)


# ---------------------------------------------------------------------------
# Diagnostics engine
# ---------------------------------------------------------------------------

def run_diagnostics(main: Dict[str, object], scan_rows: List[Dict[str, str]]) -> List[Tuple[str, str]]:
    """Return list of (severity, message). severity: 'bad', 'warn', 'info'."""
    issues: List[Tuple[str, str]] = []
    latest = main.get("latest", {}) or {}
    ping_vals: List[float] = main.get("ping_vals", [])
    loss_vals: List[float] = main.get("loss_vals", [])
    rssi_vals: List[float] = main.get("rssi_vals", [])
    snr_vals: List[float] = main.get("snr_vals", [])
    tx_vals: List[float] = main.get("tx_vals", [])
    dns_vals: List[float] = main.get("dns_vals", [])
    gw_ping_vals: List[float] = main.get("gw_ping_vals", [])
    jitter_vals: List[float] = main.get("jitter_vals", [])
    cpu_vals: List[float] = main.get("cpu_vals", [])
    mem_vals: List[float] = main.get("mem_vals", [])
    ierrs_vals: List[float] = main.get("if_ierrs_vals", [])
    oerrs_vals: List[float] = main.get("if_oerrs_vals", [])
    bssid_set: set = main.get("bssid_set", set())
    channel_set: set = main.get("channel_set", set())

    # -- WiFi signal issues --
    rssi_now = to_float(latest.get("rssi_dBm", ""))
    if rssi_now is not None:
        if rssi_now < -75:
            issues.append(("bad", f"Very weak WiFi signal: {rssi_now:.0f} dBm"))
        elif rssi_now < -67:
            issues.append(("warn", f"Weak WiFi signal: {rssi_now:.0f} dBm"))

    snr_now = to_float(latest.get("snr_dB", ""))
    if snr_now is not None and snr_now < 20:
        issues.append(("warn", f"Low SNR: {snr_now:.0f} dB (noisy environment)"))

    # -- Latency / jitter --
    if ping_vals:
        recent_ping = ping_vals[-5:]
        recent_avg = sum(recent_ping) / len(recent_ping)
        if recent_avg > 100:
            issues.append(("bad", f"High latency: {recent_avg:.0f} ms avg (last 5)"))
        elif recent_avg > 50:
            issues.append(("warn", f"Elevated latency: {recent_avg:.0f} ms avg (last 5)"))

    if jitter_vals:
        recent_jitter = jitter_vals[-5:]
        jitter_avg = sum(recent_jitter) / len(recent_jitter)
        if jitter_avg > 30:
            issues.append(("bad", f"High jitter: {jitter_avg:.0f} ms (bad for video calls)"))
        elif jitter_avg > 10:
            issues.append(("warn", f"Moderate jitter: {jitter_avg:.1f} ms"))

    # -- Packet loss --
    if loss_vals:
        recent_loss = loss_vals[-10:]
        loss_events = sum(1 for v in recent_loss if v > 0)
        if loss_events > 5:
            issues.append(("bad", f"Frequent packet loss: {loss_events}/10 recent samples"))
        elif loss_events > 0:
            pct = sum(recent_loss) / len(recent_loss)
            issues.append(("warn", f"Packet loss detected: {pct:.1f}% avg recent"))

    # -- Gateway vs internet (isolate WiFi from ISP) --
    if gw_ping_vals and ping_vals:
        gw_recent = gw_ping_vals[-5:]
        inet_recent = ping_vals[-5:]
        gw_avg = sum(gw_recent) / len(gw_recent)
        inet_avg = sum(inet_recent) / len(inet_recent)
        if gw_avg > 20 and inet_avg > 50:
            issues.append(("bad", f"Gateway latency {gw_avg:.0f}ms -> WiFi/LAN problem"))
        elif inet_avg > gw_avg * 3 and inet_avg > 50:
            issues.append(("warn", f"ISP issue: gateway {gw_avg:.0f}ms vs internet {inet_avg:.0f}ms"))

    # -- DNS --
    if dns_vals:
        recent_dns = dns_vals[-5:]
        dns_avg = sum(recent_dns) / len(recent_dns)
        if dns_avg > 200:
            issues.append(("bad", f"Slow DNS: {dns_avg:.0f} ms avg"))
        elif dns_avg > 80:
            issues.append(("warn", f"Elevated DNS latency: {dns_avg:.0f} ms"))

    # -- TX rate drops (relative to session peak) --
    if tx_vals and len(tx_vals) >= 3:
        tx_peak = max(tx_vals)
        recent_tx = tx_vals[-3:]
        tx_now = sum(recent_tx) / len(recent_tx)
        if tx_peak > 0:
            drop_pct = (tx_peak - tx_now) / tx_peak * 100
            if drop_pct >= 70:
                issues.append(("bad", f"TX rate dropped {drop_pct:.0f}%: {tx_now:.0f} of {tx_peak:.0f} Mbps"))
            elif drop_pct >= 50:
                issues.append(("warn", f"TX rate dropped {drop_pct:.0f}%: {tx_now:.0f} of {tx_peak:.0f} Mbps"))

    # -- MCS index drops --
    mcs_vals: List[float] = main.get("mcs_vals", [])
    if len(mcs_vals) >= 5:
        recent_mcs = mcs_vals[-5:]
        mcs_min = min(recent_mcs)
        mcs_max = max(mcs_vals)
        if mcs_max - mcs_min >= 4 and mcs_min < 5:
            issues.append(("warn", f"MCS rate drop: {int(mcs_max)} \u2192 {int(mcs_min)} (interference)"))

    # -- Channel band --
    band = latest.get("channel_band", "")
    if band == "2.4":
        issues.append(("warn", "On 2.4 GHz band (slower, more interference)"))

    # -- DFS channel warning --
    channel_str = latest.get("channel", "")
    ch_num = to_float(channel_str)
    if ch_num is not None:
        ch_int = int(ch_num)
        if (52 <= ch_int <= 64) or (100 <= ch_int <= 144):
            issues.append(("warn", f"DFS channel {ch_int} \u2014 radar events can disrupt calls"))

    # -- Band changes (5 GHz → 2.4 GHz) --
    band_set: set = main.get("band_set", set())
    if "2.4" in band_set and "5" in band_set:
        issues.append(("bad", "Band switch detected: moved between 5 GHz and 2.4 GHz"))

    # -- Wide channel + problems --
    ch_width = latest.get("channel_width", "")
    width_num = to_float(ch_width)
    if width_num is not None and width_num >= 80:
        has_signal_issues = (rssi_now is not None and rssi_now < -65) or (snr_now is not None and snr_now < 25)
        recent_loss = loss_vals[-10:] if loss_vals else []
        has_frequent_loss = sum(1 for v in recent_loss if v > 0) >= 3
        if has_signal_issues or has_frequent_loss:
            issues.append(("warn", f"{int(width_num)} MHz channel width \u2014 try 40 MHz for stability"))

    # -- Roaming --
    if len(bssid_set) > 1:
        issues.append(("info", f"AP roaming detected: {len(bssid_set)} different BSSIDs"))

    # -- Channel changes --
    if len(channel_set) > 1:
        issues.append(("warn", f"Channel changes: {', '.join(sorted(channel_set))}"))

    # -- Interface errors --
    total_ierrs = sum(ierrs_vals) if ierrs_vals else 0
    total_oerrs = sum(oerrs_vals) if oerrs_vals else 0
    if total_ierrs > 10 or total_oerrs > 10:
        issues.append(("warn", f"Interface errors: {int(total_ierrs)} in / {int(total_oerrs)} out"))

    # -- System resources --
    if cpu_vals:
        cpu_now = cpu_vals[-1]
        if cpu_now > 400:
            issues.append(("bad", f"Very high CPU: {cpu_now:.0f}% (may throttle WiFi)"))
        elif cpu_now > 200:
            issues.append(("warn", f"High CPU: {cpu_now:.0f}%"))
    if mem_vals:
        mem_now = mem_vals[-1]
        if mem_now > 90:
            issues.append(("bad", f"Memory pressure: {mem_now:.0f}% used"))
        elif mem_now > 80:
            issues.append(("warn", f"High memory: {mem_now:.0f}% used"))

    # -- AWDL active (only warn if latency spikes are present) --
    awdl = latest.get("awdl_status", "")
    if awdl == "active" and ping_vals and len(ping_vals) >= 5:
        recent = ping_vals[-10:]
        avg_ping = sum(recent) / len(recent)
        spikes = sum(1 for v in recent if v > avg_ping * 2 and v > 30)
        if spikes >= 2:
            issues.append(("warn", "AWDL active (AirDrop/Handoff) \u2014 may cause periodic lag spikes"))

    # -- Channel utilization (CCA) --
    cca_vals: List[float] = main.get("cca_vals", [])
    if cca_vals:
        recent_cca = cca_vals[-5:]
        cca_avg = sum(recent_cca) / len(recent_cca)
        if cca_avg > 70:
            issues.append(("bad", f"High channel utilization: {cca_avg:.0f}% (congested)"))
        elif cca_avg > 40:
            issues.append(("warn", f"Moderate channel utilization: {cca_avg:.0f}%"))

    # -- WiFi congestion from scan --
    if scan_rows:
        my_channel = latest.get("channel", "")
        same_ch = [r for r in scan_rows if r.get("channel", "").split(",")[0] == my_channel and my_channel]
        if len(same_ch) > 3:
            issues.append(("bad", f"Channel congestion: {len(same_ch)} networks on ch {my_channel}"))
        elif len(same_ch) > 1:
            issues.append(("warn", f"{len(same_ch)} networks sharing channel {my_channel}"))

    if not issues:
        issues.append(("ok", "No issues detected"))

    return issues


# ---------------------------------------------------------------------------
# Diagnostics logging
# ---------------------------------------------------------------------------

DIAG_CSV_HEADER = "timestamp,severity,message"


def log_diagnostics(
    diag_file: Path,
    diag: List[Tuple[str, str]],
    prev_msgs: set,
) -> set:
    """Append new diagnostic entries to the diagnostics CSV.

    Only logs entries whose (severity, message) pair wasn't in prev_msgs,
    so repeated diagnostics across refresh cycles don't flood the file.
    When a diagnostic disappears, the next time it reappears it will be
    logged again — giving a clear timeline of when issues come and go.

    Returns the current set of (severity, message) tuples for the next cycle.
    """
    current_msgs = {(sev, msg) for sev, msg in diag if sev != "ok"}
    new_msgs = current_msgs - prev_msgs
    gone_msgs = prev_msgs - current_msgs

    if not new_msgs and not gone_msgs:
        return current_msgs

    ts = time.strftime("%Y-%m-%d %H:%M:%S")
    write_header = not diag_file.exists() or diag_file.stat().st_size == 0

    try:
        with open(diag_file, "a", newline="") as f:
            writer = csv.writer(f)
            if write_header:
                writer.writerow(DIAG_CSV_HEADER.split(","))
            for sev, msg in sorted(gone_msgs):
                writer.writerow([ts, "resolved", msg])
            for sev, msg in sorted(new_msgs):
                writer.writerow([ts, sev, msg])
    except OSError:
        pass  # never crash the TUI over logging

    return current_msgs


# ---------------------------------------------------------------------------
# Curses helpers
# ---------------------------------------------------------------------------

def safe_add(stdscr: curses.window, row: int, col: int, text: str, attr: int = 0) -> None:
    h, w = stdscr.getmaxyx()
    if row < 0 or row >= h or col >= w:
        return
    clipped = text[: max(0, w - col - 1)]
    try:
        stdscr.addstr(row, col, clipped, attr)
    except curses.error:
        pass


def init_theme() -> Dict[str, int]:
    theme = {
        "title": curses.A_BOLD,
        "border": curses.A_DIM,
        "header": curses.A_BOLD,
        "ok": curses.A_BOLD,
        "warn": curses.A_BOLD,
        "bad": curses.A_BOLD,
        "dim": curses.A_DIM,
        "text": 0,
        "info": curses.A_BOLD,
        "spark": 0,
    }

    if not curses.has_colors():
        return theme

    try:
        curses.start_color()
        curses.use_default_colors()
        curses.init_pair(1, curses.COLOR_CYAN, -1)
        curses.init_pair(2, curses.COLOR_BLUE, -1)
        curses.init_pair(3, curses.COLOR_WHITE, -1)
        curses.init_pair(4, curses.COLOR_GREEN, -1)
        curses.init_pair(5, curses.COLOR_YELLOW, -1)
        curses.init_pair(6, curses.COLOR_RED, -1)
        curses.init_pair(7, curses.COLOR_WHITE, -1)
        curses.init_pair(8, curses.COLOR_MAGENTA, -1)
    except curses.error:
        return theme

    theme.update({
        "title": curses.color_pair(1) | curses.A_BOLD,
        "border": curses.color_pair(2),
        "header": curses.color_pair(3) | curses.A_BOLD,
        "ok": curses.color_pair(4) | curses.A_BOLD,
        "warn": curses.color_pair(5) | curses.A_BOLD,
        "bad": curses.color_pair(6) | curses.A_BOLD,
        "dim": curses.A_DIM,
        "text": 0,
        "info": curses.color_pair(1),
        "spark": curses.color_pair(8),
    })
    return theme


def value_attr(theme: Dict[str, int], kind: str, value: Optional[float]) -> int:
    if value is None:
        return theme["dim"]

    thresholds = {
        "ping":   (100, 50, False),
        "loss":   (5, 0, False),
        "rssi":   (-72, -60, True),
        "snr":    (15, 25, True),
        "tx":     (20, 50, True),
        "dns":    (200, 80, False),
        "gw":     (20, 5, False),
        "jitter": (30, 10, False),
        "cpu":    (300, 150, False),
        "mem":    (90, 80, False),
    }

    if kind not in thresholds:
        return theme["text"]

    bad_th, warn_th, inverted = thresholds[kind]
    if inverted:
        if value < bad_th:
            return theme["bad"]
        if value < warn_th:
            return theme["warn"]
        return theme["ok"]
    else:
        if value > bad_th:
            return theme["bad"]
        if value > warn_th:
            return theme["warn"]
        return theme["ok"]


# ---------------------------------------------------------------------------
# Box drawing
# ---------------------------------------------------------------------------

def draw_box(stdscr: curses.window, y: int, x: int, h: int, w: int, title: str, theme: Dict[str, int]) -> None:
    if h < 3 or w < 4:
        return
    top = "+" + "-" * (w - 2) + "+"
    mid = "|" + " " * (w - 2) + "|"
    safe_add(stdscr, y, x, top, theme["border"])
    for row in range(1, h - 1):
        safe_add(stdscr, y + row, x, mid, theme["border"])
    safe_add(stdscr, y + h - 1, x, top, theme["border"])
    safe_add(stdscr, y, x + 2, f"[ {title} ]", theme["header"])


def box_write(stdscr, y, x, h, w, row, text, attr=0):
    if row < 0 or row >= h - 2:
        return
    inner_w = max(0, w - 2)
    safe_add(stdscr, y + 1 + row, x + 1, text[:inner_w].ljust(inner_w), attr)


def box_kv(stdscr, y, x, h, w, row, label, value, theme, value_style, label_w=14):
    if row < 0 or row >= h - 2:
        return
    inner_w = max(0, w - 2)
    label_text = f"{label:<{label_w}}"
    value_w = max(0, inner_w - len(label_text) - 1)
    row_y = y + 1 + row
    safe_add(stdscr, row_y, x + 1, label_text, theme["text"])
    safe_add(stdscr, row_y, x + 1 + len(label_text) + 1, value[:value_w].rjust(value_w), value_style)


# ---------------------------------------------------------------------------
# Dashboard layout
# ---------------------------------------------------------------------------

def draw_dashboard(
    stdscr: curses.window,
    main_file: Path,
    pid_file: Optional[Path],
    refresh_sec: float,
    updated_at: str,
    theme: Dict[str, int],
    traffic_baseline: Dict[str, List[int]],
    conn_baseline: Dict[Tuple[str, str], List[int]],
    udp_baseline: Optional[Dict[str, List[int]]] = None,
) -> List[Tuple[str, str]]:
    traffic_file, conn_file, scan_file, udp_file, _diag_file = resolve_related(main_file)
    main = parse_main_csv(main_file)
    traffic = top_traffic_rows(
        subtract_traffic_totals(parse_traffic_totals(traffic_file), traffic_baseline)
    )
    connections = top_connection_rows(
        subtract_connection_totals(parse_connection_totals(conn_file), conn_baseline)
    )
    udp_traffic = top_udp_rows(
        subtract_udp_totals(parse_udp_totals(udp_file), udp_baseline or {})
    )
    scan_rows = parse_scan_csv(scan_file)
    running, pid = collector_status(pid_file)
    diag = run_diagnostics(main, scan_rows)

    stdscr.erase()
    h, w = stdscr.getmaxyx()

    if h < 24 or w < 120:
        safe_add(stdscr, 0, 0, "Terminal too small for monitor layout.", theme["bad"])
        safe_add(stdscr, 1, 0, f"Need at least 120x24, have {w}x{h}.", theme["warn"])
        safe_add(stdscr, 3, 0, "q=quit  r=reload latest", theme["dim"])
        stdscr.refresh()
        return diag

    latest = main.get("latest", {}) or {}

    # Extract all value lists
    ping_vals: List[float] = main["ping_vals"]
    loss_vals: List[float] = main["loss_vals"]
    rssi_vals: List[float] = main["rssi_vals"]
    snr_vals: List[float] = main["snr_vals"]
    tx_vals: List[float] = main["tx_vals"]
    dns_vals: List[float] = main["dns_vals"]
    gw_ping_vals: List[float] = main["gw_ping_vals"]
    jitter_vals: List[float] = main["jitter_vals"]
    cpu_vals: List[float] = main["cpu_vals"]
    mem_vals: List[float] = main["mem_vals"]

    ping_avg = avg(ping_vals)
    ping_min = min(ping_vals) if ping_vals else None
    ping_max = max(ping_vals) if ping_vals else None
    loss_avg = avg(loss_vals)
    rssi_avg = avg(rssi_vals)
    snr_avg = avg(snr_vals)
    tx_avg = avg(tx_vals)
    dns_avg = avg(dns_vals)
    dns_max = max(dns_vals) if dns_vals else None
    gw_avg = avg(gw_ping_vals)
    jitter_avg = avg(jitter_vals)

    # -- Title bar --
    title = " netmon monitor "
    controls = " q=quit  r=reload latest "
    safe_add(stdscr, 0, 1, title, theme["title"])
    safe_add(stdscr, 0, max(1, w - len(controls) - 2), controls, theme["text"])

    # -- Layout: 3 columns on top, 2 + diagnostics on bottom --
    # Row 0: title bar
    # Row 1-8: top row (3 boxes: Session | Health | WiFi Details)
    # Row 9-16: mid row (Gateway/System | Processes | Connections)
    # Row 17+: bottom row (Diagnostics | WiFi Environment)

    gap = 1
    col3_w = (w - 2 * gap) // 3
    col3_extra = w - 2 * gap - 3 * col3_w
    c1_x = 0
    c1_w = col3_w
    c2_x = col3_w + gap
    c2_w = col3_w
    c3_x = 2 * (col3_w + gap)
    c3_w = col3_w + col3_extra

    half_w_left = (w - gap) // 2
    half_w_right = w - gap - half_w_left

    top_y = 1
    top_h = 10
    mid_y = top_y + top_h
    mid_h = max(8, (h - mid_y) // 2)
    bot_y = mid_y + mid_h
    bot_h = h - bot_y

    # ===== TOP ROW: Session | Health | WiFi Details =====
    draw_box(stdscr, top_y, c1_x, top_h, c1_w, "Session", theme)
    draw_box(stdscr, top_y, c2_x, top_h, c2_w, "Health", theme)
    draw_box(stdscr, top_y, c3_x, top_h, c3_w, "WiFi Details", theme)

    # -- Session box --
    status_txt = "RUNNING" if running else "STOPPED"
    status_attr = theme["ok"] if running else theme["bad"]
    if pid is not None:
        status_txt = f"{status_txt} (pid {pid})"

    duration = calc_duration(str(main["first_ts"]), str(main["last_ts"]))

    box_kv(stdscr, top_y, c1_x, top_h, c1_w, 0, "Collector", status_txt, theme, status_attr)
    box_kv(stdscr, top_y, c1_x, top_h, c1_w, 1, "Samples", str(main["samples"]), theme, theme["text"])
    box_kv(stdscr, top_y, c1_x, top_h, c1_w, 2, "Duration", duration, theme, theme["text"])
    box_kv(stdscr, top_y, c1_x, top_h, c1_w, 3, "Period",
           f"{main['first_ts']} -> {main['last_ts']}", theme, theme["text"])
    box_kv(stdscr, top_y, c1_x, top_h, c1_w, 4, "Refresh", f"{refresh_sec:.1f}s", theme, theme["text"])
    box_kv(stdscr, top_y, c1_x, top_h, c1_w, 5, "Updated", updated_at, theme, theme["text"])
    box_kv(stdscr, top_y, c1_x, top_h, c1_w, 6, "Interface",
           latest.get("interface", "?") + " / " + latest.get("local_ip", "?"), theme, theme["text"])
    box_kv(stdscr, top_y, c1_x, top_h, c1_w, 7, "Log", main_file.name, theme, theme["text"])

    # -- Health box --
    r = 0
    box_kv(stdscr, top_y, c2_x, top_h, c2_w, r, "Ping avg",
           fmt_num(ping_avg, " ms") + "  " + sparkline(ping_vals, 12),
           theme, value_attr(theme, "ping", ping_avg)); r += 1
    box_kv(stdscr, top_y, c2_x, top_h, c2_w, r, "Ping min/max",
           f"{fmt_num(ping_min, ' ms')} / {fmt_num(ping_max, ' ms')}",
           theme, theme["text"]); r += 1
    box_kv(stdscr, top_y, c2_x, top_h, c2_w, r, "Loss avg",
           fmt_num(loss_avg, "%") + "  " + sparkline(loss_vals, 12),
           theme, value_attr(theme, "loss", loss_avg)); r += 1
    box_kv(stdscr, top_y, c2_x, top_h, c2_w, r, "Jitter avg",
           fmt_num(jitter_avg, " ms") + "  " + sparkline(jitter_vals, 12),
           theme, value_attr(theme, "jitter", jitter_avg)); r += 1
    box_kv(stdscr, top_y, c2_x, top_h, c2_w, r, "DNS avg/max",
           f"{fmt_num(dns_avg, ' ms')} / {fmt_num(dns_max, ' ms')}",
           theme, value_attr(theme, "dns", dns_avg)); r += 1
    box_kv(stdscr, top_y, c2_x, top_h, c2_w, r, "Gateway",
           fmt_num(gw_avg, " ms") + "  " + sparkline(gw_ping_vals, 12),
           theme, value_attr(theme, "gw", gw_avg)); r += 1
    box_kv(stdscr, top_y, c2_x, top_h, c2_w, r, "Ping target",
           str(main["ping_target"]) + " / gw " + latest.get("gateway_ip", "?"),
           theme, theme["text"]); r += 1

    # -- WiFi Details box --
    r = 0
    raw_ssid = latest.get("ssid", "?")
    ssid_display = raw_ssid if raw_ssid not in ("unknown", "?", "") else "(restricted by macOS)"
    ssid_style = theme["text"] if raw_ssid not in ("unknown", "?", "") else theme["dim"]
    box_kv(stdscr, top_y, c3_x, top_h, c3_w, r, "SSID",
           ssid_display, theme, ssid_style); r += 1
    raw_bssid = latest.get("bssid", "?")
    bssid_display = raw_bssid if raw_bssid not in ("?", "") else "(restricted by macOS)"
    bssid_style = theme["text"] if raw_bssid not in ("?", "") else theme["dim"]
    box_kv(stdscr, top_y, c3_x, top_h, c3_w, r, "BSSID",
           bssid_display, theme, bssid_style); r += 1
    ch_str = latest.get("channel", "?")
    band = latest.get("channel_band", "?")
    ch_w = latest.get("channel_width", "?")
    ch_display = f"ch {ch_str}"
    if band and band != "?":
        ch_display += f" ({band} GHz)"
    if ch_w and ch_w != "?":
        ch_display += f" {ch_w}MHz"
    box_kv(stdscr, top_y, c3_x, top_h, c3_w, r, "Channel",
           ch_display, theme, theme["text"]); r += 1
    box_kv(stdscr, top_y, c3_x, top_h, c3_w, r, "RSSI / SNR",
           f"{fmt_num(rssi_avg, ' dBm')} / {fmt_num(snr_avg, ' dB')}  {sparkline(rssi_vals, 10)}",
           theme, value_attr(theme, "rssi", rssi_avg)); r += 1
    box_kv(stdscr, top_y, c3_x, top_h, c3_w, r, "TX rate",
           f"{fmt_num(tx_avg, ' Mbps')}  {sparkline(tx_vals, 10)}",
           theme, value_attr(theme, "tx", tx_avg)); r += 1
    mcs_val = latest.get("mcs", "?")
    mcs_txt = mcs_val if mcs_val and mcs_val != "?" else "n/a"
    box_kv(stdscr, top_y, c3_x, top_h, c3_w, r, "MCS index",
           mcs_txt, theme, na_style(theme, mcs_txt, theme["text"])); r += 1
    bssid_count = len(main.get("bssid_set", set()))
    roam_txt = f"{bssid_count} AP(s) seen" if bssid_count > 1 else "stable"
    box_kv(stdscr, top_y, c3_x, top_h, c3_w, r, "Roaming",
           roam_txt, theme, theme["warn"] if bssid_count > 1 else theme["ok"]); r += 1

    # ===== MID ROW: System | Processes | Connections =====
    sys_w = col3_w
    proc_w = col3_w
    conn_w = col3_w + col3_extra

    draw_box(stdscr, mid_y, c1_x, mid_h, sys_w, "System / Gateway", theme)
    draw_box(stdscr, mid_y, c2_x, mid_h, proc_w, "Top Processes", theme)
    draw_box(stdscr, mid_y, c3_x, mid_h, conn_w, "Top Connections", theme)

    # -- System box --
    r = 0
    cpu_now = cpu_vals[-1] if cpu_vals else None
    mem_now = mem_vals[-1] if mem_vals else None
    box_kv(stdscr, mid_y, c1_x, mid_h, sys_w, r, "CPU total",
           fmt_num(cpu_now, "%") + "  " + sparkline(cpu_vals, 12),
           theme, value_attr(theme, "cpu", cpu_now)); r += 1
    box_kv(stdscr, mid_y, c1_x, mid_h, sys_w, r, "Memory",
           fmt_num(mem_now, "% used") + "  " + sparkline(mem_vals, 10),
           theme, value_attr(theme, "mem", mem_now)); r += 1
    box_kv(stdscr, mid_y, c1_x, mid_h, sys_w, r, "Gateway IP",
           latest.get("gateway_ip", "?"), theme, theme["text"]); r += 1
    box_kv(stdscr, mid_y, c1_x, mid_h, sys_w, r, "GW latency",
           fmt_num(gw_avg, " ms") + "  " + sparkline(gw_ping_vals, 12),
           theme, value_attr(theme, "gw", gw_avg)); r += 1
    box_kv(stdscr, mid_y, c1_x, mid_h, sys_w, r, "Public IP",
           latest.get("public_ip", "?"), theme, theme["text"]); r += 1
    total_ierrs = sum(main.get("if_ierrs_vals", []))
    total_oerrs = sum(main.get("if_oerrs_vals", []))
    err_style = theme["warn"] if (total_ierrs > 0 or total_oerrs > 0) else theme["ok"]
    box_kv(stdscr, mid_y, c1_x, mid_h, sys_w, r, "IF errors",
           f"{int(total_ierrs)} in / {int(total_oerrs)} out", theme, err_style); r += 1

    # -- Processes box (TCP + UDP) --
    proc_inner_w = max(1, proc_w - 2)
    recv_w = 9
    sent_w = 9
    retx_w = 6
    pname_w = max(8, proc_inner_w - (recv_w + sent_w + retx_w + 9))
    max_proc_rows = max(0, mid_h - 3)
    row_idx = 0

    # TCP section
    tcp_label = f"{' TCP ':-^{proc_inner_w}}"
    box_write(stdscr, mid_y, c2_x, mid_h, proc_w, row_idx, tcp_label, theme["header"])
    row_idx += 1
    tcp_hdr = f"{'Process':<{pname_w}} | {'Recv':>{recv_w}} | {'Sent':>{sent_w}} | {'ReTX':>{retx_w}}"
    box_write(stdscr, mid_y, c2_x, mid_h, proc_w, row_idx, tcp_hdr, theme["dim"])
    row_idx += 1
    # Reserve space for UDP: label + header + at least 1 row = 3 rows
    tcp_limit = min(len(traffic), max(1, max_proc_rows - row_idx - 3)) if udp_traffic else max_proc_rows - row_idx
    for proc, recv, sent, retx in traffic[:tcp_limit]:
        if row_idx >= max_proc_rows:
            break
        retx_str = "-" if retx == 0 else str(retx)
        line = f"{proc[:pname_w]:<{pname_w}} | {human_bytes(recv):>{recv_w}} | {human_bytes(sent):>{sent_w}} | {retx_str:>{retx_w}}"
        row_attr = theme["warn"] if retx > 100 else theme["text"]
        box_write(stdscr, mid_y, c2_x, mid_h, proc_w, row_idx, line, row_attr)
        row_idx += 1

    # UDP section
    if udp_traffic and row_idx < max_proc_rows - 1:
        udp_label = f"{' UDP ':-^{proc_inner_w}}"
        box_write(stdscr, mid_y, c2_x, mid_h, proc_w, row_idx, udp_label, theme["header"])
        row_idx += 1
        udp_hdr = f"{'Process':<{pname_w}} | {'Recv':>{recv_w}} | {'Sent':>{sent_w}} |"
        box_write(stdscr, mid_y, c2_x, mid_h, proc_w, row_idx, udp_hdr, theme["dim"])
        row_idx += 1
        for proc, recv, sent in udp_traffic:
            if row_idx >= max_proc_rows:
                break
            line = f"{proc[:pname_w]:<{pname_w}} | {human_bytes(recv):>{recv_w}} | {human_bytes(sent):>{sent_w}} |"
            box_write(stdscr, mid_y, c2_x, mid_h, proc_w, row_idx, line, theme["text"])
            row_idx += 1

    # -- Connections box --
    conn_inner_w = max(1, conn_w - 2)
    cr_w = 9
    cs_w = 9
    ct_w = 6
    dynamic_w = max(16, conn_inner_w - (cr_w + cs_w + ct_w + 12))
    cp_w = max(8, dynamic_w // 2)
    crm_w = max(8, dynamic_w - cp_w)
    conn_header = (
        f"{'Process':<{cp_w}} | {'Remote':<{crm_w}} | "
        f"{'Recv':>{cr_w}} | {'Sent':>{cs_w}} | {'ReTX':>{ct_w}}"
    )
    box_write(stdscr, mid_y, c3_x, mid_h, conn_w, 0, conn_header, theme["header"])
    max_conn_rows = max(0, mid_h - 3)
    for i, (proc, remote, recv, sent, retx) in enumerate(connections[:max_conn_rows], start=1):
        retx_str = "-" if retx == 0 else str(retx)
        line = (
            f"{proc[:cp_w]:<{cp_w}} | {remote[:crm_w]:<{crm_w}} | "
            f"{human_bytes(recv):>{cr_w}} | {human_bytes(sent):>{cs_w}} | {retx_str:>{ct_w}}"
        )
        row_attr = theme["warn"] if retx > 100 else theme["text"]
        box_write(stdscr, mid_y, c3_x, mid_h, conn_w, i, line, row_attr)

    # ===== BOTTOM ROW: Diagnostics | WiFi Environment =====
    if bot_h >= 4:
        diag_w = half_w_left
        env_w = half_w_right

        draw_box(stdscr, bot_y, 0, bot_h, diag_w, "Diagnostics", theme)
        draw_box(stdscr, bot_y, half_w_left + gap, bot_h, env_w, "Nearby WiFi Networks", theme)

        # -- Diagnostics --
        max_diag = max(0, bot_h - 2)
        for i, (sev, msg) in enumerate(diag[:max_diag]):
            if sev == "bad":
                icon = "!!"
                attr = theme["bad"]
            elif sev == "warn":
                icon = " !"
                attr = theme["warn"]
            elif sev == "ok":
                icon = "OK"
                attr = theme["ok"]
            else:
                icon = "--"
                attr = theme["info"]
            box_write(stdscr, bot_y, 0, bot_h, diag_w, i, f" {icon} {msg}", attr)

        # -- Nearby WiFi Networks (channel-focused, SSIDs are redacted by macOS) --
        if scan_rows:
            my_channel = latest.get("channel", "")
            env_inner_w = max(1, env_w - 2)
            sec_w = max(10, env_inner_w - 38)
            env_hdr = f"{'':>3}  {'Channel':<17} {'Band':>5} {'Width':>5}  {'Security':<{sec_w}}"
            box_write(stdscr, bot_y, half_w_left + gap, bot_h, env_w, 0, env_hdr, theme["header"])

            # Parse "36 (5GHz, 80MHz)" -> ch_num, band, width
            def parse_ch_info(raw):
                parts = raw.replace("(", "").replace(")", "").replace(",", "").split()
                ch_num = parts[0] if parts else "?"
                band_str = parts[1] if len(parts) > 1 else ""
                width_str = parts[2] if len(parts) > 2 else ""
                return ch_num, band_str, width_str

            ch_counts: Dict[str, int] = defaultdict(int)
            parsed_rows = []
            for sr in scan_rows:
                raw_ch = sr.get("channel", "?")
                ch_num, band_str, width_str = parse_ch_info(raw_ch)
                ch_counts[ch_num] += 1
                parsed_rows.append((ch_num, band_str, width_str, sr.get("security", "")))

            # Sort: same-channel first, then by channel number
            def sort_key(item):
                same = 0 if item[0] == my_channel else 1
                try:
                    n = int(item[0])
                except ValueError:
                    n = 999
                return (same, n)

            parsed_rows.sort(key=sort_key)
            max_scan = max(0, bot_h - 3)
            for i, (ch_num, band_str, width_str, sec) in enumerate(parsed_rows[:max_scan], start=1):
                same = ch_num == my_channel and my_channel
                marker = ">>>" if same else f"{i:>3}"
                line = f"{marker}  ch {ch_num:<13} {band_str:>5} {width_str:>5}  {sec[:sec_w]:<{sec_w}}"
                attr = theme["warn"] if same else theme["text"]
                box_write(stdscr, bot_y, half_w_left + gap, bot_h, env_w, i, line, attr)

            # Summary line
            same_ch_count = ch_counts.get(my_channel, 0) if my_channel else 0
            total_count = len(parsed_rows)
            summary_row = min(len(parsed_rows) + 1, max_scan)
            if summary_row > 0 and summary_row <= max_scan:
                summary = f" {total_count} networks seen"
                if same_ch_count > 0 and my_channel:
                    summary += f", {same_ch_count} sharing your ch {my_channel}"
                s_attr = theme["warn"] if same_ch_count > 1 else theme["text"]
                box_write(stdscr, bot_y, half_w_left + gap, bot_h, env_w, summary_row, summary, s_attr)
        else:
            box_write(stdscr, bot_y, half_w_left + gap, bot_h, env_w, 0,
                      "Waiting for first scan (~30s)...", theme["dim"])

    stdscr.refresh()
    return diag


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------

def run_tui(stdscr: curses.window, args: argparse.Namespace) -> int:
    try:
        curses.curs_set(0)
    except curses.error:
        pass
    stdscr.nodelay(True)
    stdscr.timeout(max(100, int(args.refresh * 1000)))
    theme = init_theme()

    log_dir = Path(args.log_dir)
    main_file = Path(args.main_file) if args.main_file else latest_main_log(log_dir)
    pid_file = Path(args.pid_file) if args.pid_file else None

    if main_file is None:
        stdscr.erase()
        safe_add(stdscr, 0, 0, f"No main logs found in {log_dir}", theme["bad"])
        safe_add(stdscr, 2, 0, "Press q to exit.", theme["dim"])
        stdscr.refresh()
        while True:
            key = stdscr.getch()
            if key in (ord("q"), ord("Q")):
                return 1
            time.sleep(0.1)

    assert main_file is not None

    traffic_file, conn_file, _scan_file, udp_file, diag_file = resolve_related(main_file)
    traffic_baseline = parse_traffic_totals(traffic_file)
    conn_baseline = parse_connection_totals(conn_file)
    udp_baseline = parse_udp_totals(udp_file)
    prev_diag_msgs: set = set()

    while True:
        diag = draw_dashboard(
            stdscr,
            main_file,
            pid_file,
            args.refresh,
            time.strftime("%H:%M:%S"),
            theme,
            traffic_baseline,
            conn_baseline,
            udp_baseline,
        )
        prev_diag_msgs = log_diagnostics(diag_file, diag, prev_diag_msgs)
        key = stdscr.getch()
        if key in (ord("q"), ord("Q")):
            return 0
        if key in (ord("r"), ord("R")):
            latest = latest_main_log(log_dir)
            if latest is not None:
                main_file = latest
                traffic_file, conn_file, _scan_file, udp_file, diag_file = resolve_related(main_file)
                traffic_baseline = parse_traffic_totals(traffic_file)
                conn_baseline = parse_connection_totals(conn_file)
                udp_baseline = parse_udp_totals(udp_file)
                prev_diag_msgs = set()


def main() -> int:
    args = parse_args()

    def _sigint_handler(_sig, _frame):
        raise KeyboardInterrupt

    signal.signal(signal.SIGINT, _sigint_handler)
    signal.signal(signal.SIGTERM, _sigint_handler)

    try:
        return curses.wrapper(lambda stdscr: run_tui(stdscr, args))
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    sys.exit(main())
