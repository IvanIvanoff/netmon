#!/usr/bin/env python3
"""
netmon_tui.py

Live ncurses dashboard for netmon CSV logs.
Reads existing CSV files and refreshes statistics in-place.
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


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Live TUI for netmon logs")
    parser.add_argument("--main-file", default="", help="Main call-*.csv file to monitor")
    parser.add_argument("--log-dir", default=str(Path.home() / "call-network-logs"))
    parser.add_argument("--pid-file", default="", help="Optional PID file for collector status")
    parser.add_argument("--refresh", type=float, default=1.0, help="Refresh interval in seconds")
    return parser.parse_args()


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


def latest_main_log(log_dir: Path) -> Optional[Path]:
    candidates = []
    for path in log_dir.glob("call-*.csv"):
        name = path.name
        if name.endswith("-traffic.csv") or name.endswith("-connections.csv"):
            continue
        candidates.append(path)
    if not candidates:
        return None
    candidates.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    return candidates[0]


def resolve_related(main_file: Path) -> Tuple[Path, Path]:
    stem = str(main_file)
    if stem.endswith(".csv"):
        base = stem[:-4]
    else:
        base = stem
    return Path(f"{base}-traffic.csv"), Path(f"{base}-connections.csv")


def collector_status(pid_file: Optional[Path]) -> Tuple[bool, Optional[int]]:
    if not pid_file:
        return False, None
    if not pid_file.exists():
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

                ping = to_float(row.get("ping_avg_ms", ""))
                if ping is not None:
                    result["ping_vals"].append(ping)
                loss = to_float(row.get("loss_%", ""))
                if loss is not None:
                    result["loss_vals"].append(loss)
                rssi = to_float(row.get("rssi_dBm", ""))
                if rssi is not None:
                    result["rssi_vals"].append(rssi)
                snr = to_float(row.get("snr_dB", ""))
                if snr is not None:
                    result["snr_vals"].append(snr)
                tx = to_float(row.get("tx_rate_Mbps", ""))
                if tx is not None:
                    result["tx_vals"].append(tx)
                dns = to_float(row.get("dns_ms", ""))
                if dns is not None:
                    result["dns_vals"].append(dns)
    except Exception:
        # Keep dashboard alive on partial/truncated writes.
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


def fmt_num(v: Optional[float], suffix: str = "") -> str:
    if v is None:
        return "n/a"
    if abs(v) >= 100:
        return f"{v:.0f}{suffix}"
    return f"{v:.1f}{suffix}"


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
    }

    if not curses.has_colors():
        return theme

    try:
        curses.start_color()
        curses.use_default_colors()
        curses.init_pair(1, curses.COLOR_CYAN, -1)   # title
        curses.init_pair(2, curses.COLOR_BLUE, -1)   # border
        curses.init_pair(3, curses.COLOR_WHITE, -1)  # headers
        curses.init_pair(4, curses.COLOR_GREEN, -1)  # ok
        curses.init_pair(5, curses.COLOR_YELLOW, -1) # warn
        curses.init_pair(6, curses.COLOR_RED, -1)    # bad
        curses.init_pair(7, curses.COLOR_BLACK, -1)  # dim
    except curses.error:
        return theme

    theme.update(
        {
            "title": curses.color_pair(1) | curses.A_BOLD,
            "border": curses.color_pair(2),
            "header": curses.color_pair(3) | curses.A_BOLD,
            "ok": curses.color_pair(4) | curses.A_BOLD,
            "warn": curses.color_pair(5) | curses.A_BOLD,
            "bad": curses.color_pair(6) | curses.A_BOLD,
            "dim": curses.color_pair(7) | curses.A_DIM,
            "text": 0,
        }
    )
    return theme


def value_attr(theme: Dict[str, int], kind: str, value: Optional[float]) -> int:
    if value is None:
        return theme["dim"]

    if kind == "ping":
        if value > 100:
            return theme["bad"]
        if value > 50:
            return theme["warn"]
        return theme["ok"]

    if kind == "loss":
        if value > 5:
            return theme["bad"]
        if value > 0:
            return theme["warn"]
        return theme["ok"]

    if kind == "rssi":
        if value < -72:
            return theme["bad"]
        if value < -60:
            return theme["warn"]
        return theme["ok"]

    if kind == "snr":
        if value < 15:
            return theme["bad"]
        if value < 25:
            return theme["warn"]
        return theme["ok"]

    if kind == "tx":
        if value < 80:
            return theme["bad"]
        if value < 200:
            return theme["warn"]
        return theme["ok"]

    if kind == "dns":
        if value > 200:
            return theme["bad"]
        if value > 80:
            return theme["warn"]
        return theme["ok"]

    return theme["text"]


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


def box_write(
    stdscr: curses.window,
    y: int,
    x: int,
    h: int,
    w: int,
    row: int,
    text: str,
    attr: int = 0,
) -> None:
    if row < 0 or row >= h - 2:
        return
    inner_w = max(0, w - 2)
    safe_add(stdscr, y + 1 + row, x + 1, text[:inner_w].ljust(inner_w), attr)


def box_kv(
    stdscr: curses.window,
    y: int,
    x: int,
    h: int,
    w: int,
    row: int,
    label: str,
    value: str,
    theme: Dict[str, int],
    value_style: int,
) -> None:
    if row < 0 or row >= h - 2:
        return
    inner_w = max(0, w - 2)
    label_text = f"{label:<12}"
    value_w = max(0, inner_w - len(label_text) - 1)
    row_y = y + 1 + row
    safe_add(stdscr, row_y, x + 1, label_text, theme["text"])
    safe_add(stdscr, row_y, x + 1 + len(label_text), " ", theme["text"])
    safe_add(stdscr, row_y, x + 1 + len(label_text) + 1, value[:value_w].rjust(value_w), value_style)


def draw_dashboard(
    stdscr: curses.window,
    main_file: Path,
    pid_file: Optional[Path],
    refresh_sec: float,
    updated_at: str,
    theme: Dict[str, int],
    traffic_baseline: Dict[str, List[int]],
    conn_baseline: Dict[Tuple[str, str], List[int]],
) -> None:
    traffic_file, conn_file = resolve_related(main_file)
    main = parse_main_csv(main_file)
    traffic = top_traffic_rows(
        subtract_traffic_totals(parse_traffic_totals(traffic_file), traffic_baseline)
    )
    connections = top_connection_rows(
        subtract_connection_totals(parse_connection_totals(conn_file), conn_baseline)
    )
    running, pid = collector_status(pid_file)

    stdscr.erase()
    h, w = stdscr.getmaxyx()

    if h < 18 or w < 90:
        safe_add(stdscr, 0, 0, "Terminal too small for monitor layout.", theme["bad"])
        safe_add(stdscr, 1, 0, "Resize terminal to at least 90x18.", theme["warn"])
        safe_add(stdscr, 3, 0, "q=quit  r=reload latest", theme["dim"])
        stdscr.refresh()
        return

    ping_vals: List[float] = main["ping_vals"]  # type: ignore[assignment]
    loss_vals: List[float] = main["loss_vals"]  # type: ignore[assignment]
    rssi_vals: List[float] = main["rssi_vals"]  # type: ignore[assignment]
    snr_vals: List[float] = main["snr_vals"]  # type: ignore[assignment]
    tx_vals: List[float] = main["tx_vals"]  # type: ignore[assignment]
    dns_vals: List[float] = main["dns_vals"]  # type: ignore[assignment]

    ping_avg = avg(ping_vals)
    ping_min = min(ping_vals) if ping_vals else None
    ping_max = max(ping_vals) if ping_vals else None
    loss_avg = avg(loss_vals)
    rssi_avg = avg(rssi_vals)
    snr_avg = avg(snr_vals)
    tx_avg = avg(tx_vals)
    dns_avg = avg(dns_vals)
    dns_max = max(dns_vals) if dns_vals else None

    latest = main.get("latest", {}) or {}
    title = " netmon monitor "
    controls = " q=quit  r=reload latest "
    safe_add(stdscr, 0, 1, title, theme["title"])
    safe_add(stdscr, 0, max(1, w - len(controls) - 2), controls, theme["dim"])

    gap = 1
    left_w = (w - gap) // 2
    right_w = w - gap - left_w
    left_x = 0
    right_x = left_w + gap

    top_h = 8
    top_y = 1
    bottom_y = top_y + top_h
    bottom_h = h - bottom_y
    if bottom_h < 8:
        top_h = max(6, h - 8)
        bottom_y = top_y + top_h
        bottom_h = h - bottom_y

    draw_box(stdscr, top_y, left_x, top_h, left_w, "Session", theme)
    draw_box(stdscr, top_y, right_x, top_h, right_w, "Health", theme)
    draw_box(stdscr, bottom_y, left_x, bottom_h, left_w, "Top Processes (since monitor start)", theme)
    draw_box(stdscr, bottom_y, right_x, bottom_h, right_w, "Top Connections (since monitor start)", theme)

    status_txt = "RUNNING" if running else "STOPPED"
    status_attr = theme["ok"] if running else theme["bad"]
    if pid is not None:
        status_txt = f"{status_txt} (pid {pid})"

    box_kv(stdscr, top_y, left_x, top_h, left_w, 0, "Collector", status_txt, theme, status_attr)
    box_kv(stdscr, top_y, left_x, top_h, left_w, 1, "Samples", str(main["samples"]), theme, theme["text"])
    box_kv(
        stdscr,
        top_y,
        left_x,
        top_h,
        left_w,
        2,
        "Period",
        f"{main['first_ts']} -> {main['last_ts']}",
        theme,
        theme["text"],
    )
    box_kv(stdscr, top_y, left_x, top_h, left_w, 3, "Refresh", f"{refresh_sec:.1f}s", theme, theme["text"])
    box_kv(stdscr, top_y, left_x, top_h, left_w, 4, "Updated", updated_at, theme, theme["text"])
    box_kv(stdscr, top_y, left_x, top_h, left_w, 5, "Log", main_file.name, theme, theme["dim"])

    box_kv(stdscr, top_y, right_x, top_h, right_w, 0, "Ping tgt", str(main["ping_target"]), theme, theme["text"])
    box_kv(
        stdscr,
        top_y,
        right_x,
        top_h,
        right_w,
        1,
        "Ping avg",
        fmt_num(ping_avg, " ms"),
        theme,
        value_attr(theme, "ping", ping_avg),
    )
    box_kv(
        stdscr,
        top_y,
        right_x,
        top_h,
        right_w,
        2,
        "Ping min/max",
        f"{fmt_num(ping_min, ' ms')} / {fmt_num(ping_max, ' ms')}",
        theme,
        theme["text"],
    )
    box_kv(
        stdscr,
        top_y,
        right_x,
        top_h,
        right_w,
        3,
        "Loss avg",
        fmt_num(loss_avg, "%"),
        theme,
        value_attr(theme, "loss", loss_avg),
    )
    box_kv(
        stdscr,
        top_y,
        right_x,
        top_h,
        right_w,
        4,
        "RSSI/SNR",
        f"{fmt_num(rssi_avg, ' dBm')} / {fmt_num(snr_avg, ' dB')}",
        theme,
        value_attr(theme, "rssi", rssi_avg),
    )
    box_kv(
        stdscr,
        top_y,
        right_x,
        top_h,
        right_w,
        5,
        "TX / DNS",
        f"{fmt_num(tx_avg, ' Mbps')} / {fmt_num(dns_avg, ' ms')}",
        theme,
        value_attr(theme, "tx", tx_avg),
    )

    proc_inner_w = max(1, left_w - 2)
    recv_w = 9
    sent_w = 9
    retx_w = 7
    proc_w = max(8, proc_inner_w - (recv_w + sent_w + retx_w + 9))
    proc_header = f"{'Process':<{proc_w}} | {'Recv':>{recv_w}} | {'Sent':>{sent_w}} | {'ReTX':>{retx_w}}"
    box_write(stdscr, bottom_y, left_x, bottom_h, left_w, 0, proc_header, theme["header"])
    max_proc_rows = max(0, bottom_h - 3)
    for i, (proc, recv, sent, retx) in enumerate(traffic[:max_proc_rows], start=1):
        retx_str = "-" if retx == 0 else str(retx)
        line = f"{proc[:proc_w]:<{proc_w}} | {human_bytes(recv):>{recv_w}} | {human_bytes(sent):>{sent_w}} | {retx_str:>{retx_w}}"
        row_attr = theme["warn"] if retx > 100 else theme["text"]
        box_write(stdscr, bottom_y, left_x, bottom_h, left_w, i, line, row_attr)

    conn_inner_w = max(1, right_w - 2)
    conn_recv_w = 9
    conn_sent_w = 9
    conn_retx_w = 7
    dynamic_w = max(16, conn_inner_w - (conn_recv_w + conn_sent_w + conn_retx_w + 12))
    proc_col_w = max(8, dynamic_w // 2)
    remote_col_w = max(8, dynamic_w - proc_col_w)
    conn_header = (
        f"{'Process':<{proc_col_w}} | {'Remote':<{remote_col_w}} | "
        f"{'Recv':>{conn_recv_w}} | {'Sent':>{conn_sent_w}} | {'ReTX':>{conn_retx_w}}"
    )
    box_write(stdscr, bottom_y, right_x, bottom_h, right_w, 0, conn_header, theme["header"])
    max_conn_rows = max(0, bottom_h - 3)
    for i, (proc, remote, recv, sent, retx) in enumerate(connections[:max_conn_rows], start=1):
        retx_str = "-" if retx == 0 else str(retx)
        line = (
            f"{proc[:proc_col_w]:<{proc_col_w}} | {remote[:remote_col_w]:<{remote_col_w}} | "
            f"{human_bytes(recv):>{conn_recv_w}} | {human_bytes(sent):>{conn_sent_w}} | {retx_str:>{conn_retx_w}}"
        )
        row_attr = theme["warn"] if retx > 100 else theme["text"]
        box_write(stdscr, bottom_y, right_x, bottom_h, right_w, i, line, row_attr)

    stdscr.refresh()


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

    traffic_file, conn_file = resolve_related(main_file)
    traffic_baseline = parse_traffic_totals(traffic_file)
    conn_baseline = parse_connection_totals(conn_file)

    while True:
        draw_dashboard(
            stdscr,
            main_file,
            pid_file,
            args.refresh,
            time.strftime("%H:%M:%S"),
            theme,
            traffic_baseline,
            conn_baseline,
        )
        key = stdscr.getch()
        if key in (ord("q"), ord("Q")):
            return 0
        if key in (ord("r"), ord("R")):
            latest = latest_main_log(log_dir)
            if latest is not None:
                main_file = latest
                traffic_file, conn_file = resolve_related(main_file)
                traffic_baseline = parse_traffic_totals(traffic_file)
                conn_baseline = parse_connection_totals(conn_file)


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
