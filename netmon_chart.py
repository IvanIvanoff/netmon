#!/usr/bin/env python3
"""
netmon_chart.py — Generate an interactive HTML timeline of diagnostics.

Reads a netmon diagnostics CSV (call-*-diagnostics.csv) and the corresponding
main CSV to produce an interactive Plotly chart.

Usage:
    python3 netmon_chart.py                          # live server, auto-refresh
    python3 netmon_chart.py -o report.html           # static HTML export
    python3 netmon_chart.py --main-file call-XXX.csv # specific session
    python3 netmon_chart.py --diag-file call-XXX-diagnostics.csv
"""

from __future__ import annotations

import argparse
import html
import json
import os
import signal
import sys
import threading
import webbrowser
from functools import partial
from http.server import HTTPServer, BaseHTTPRequestHandler
from pathlib import Path
from typing import Dict, List, Optional

from netmon_common import (
    to_float, latest_main_log, resolve_related, resolve_diag_file,
    resolve_main_file, session_name as derive_session_name, read_csv_rows,
    tag_vpn,
)


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Interactive netmon diagnostics chart")
    p.add_argument("--diag-file", help="Path to diagnostics CSV directly")
    p.add_argument("--main-file", help="Path to main call-*.csv (auto-resolves diagnostics)")
    p.add_argument("--log-dir", default=str(Path.home() / "call-network-logs"),
                   help="Log directory (uses latest session)")
    p.add_argument("-o", "--output", help="Output static HTML (no live refresh)")
    p.add_argument("--no-open", action="store_true", help="Don't open browser")
    p.add_argument("--port", type=int, default=0, help="Server port (0 = auto)")
    return p.parse_args()


# Severity → visual properties
SEVERITY_COLORS = {
    "bad": "#e74c3c",
    "warn": "#f39c12",
    "info": "#3498db",
    "resolved": "#2ecc71",
}

SEVERITY_SYMBOLS = {
    "bad": "x",
    "warn": "triangle-up",
    "info": "circle",
    "resolved": "diamond",
}

SEVERITY_Y = {
    "bad": 3,
    "warn": 2,
    "info": 1,
    "resolved": 0,
}


def _make_line_trace(timestamps, vals, name, unit, color,
                     fill=None, dash=None):
    """Build a single Plotly line trace dict."""
    xs, ys = [], []
    for t, v in zip(timestamps, vals):
        if v is not None:
            xs.append(t)
            ys.append(v)
    line = {"color": color, "width": 1.5}
    if dash:
        line["dash"] = dash
    trace = {
        "x": xs, "y": ys,
        "mode": "lines", "type": "scatter",
        "name": name,
        "line": line,
        "hovertemplate": f"{name}: %{{y:.1f}} {unit}<extra></extra>",
    }
    if fill:
        trace["fill"] = fill
        trace["fillcolor"] = color.replace(")", ",0.15)").replace("rgb", "rgba") \
            if color.startswith("rgb") else color + "26"
    return trace


def _panel(title, traces, height=200, ytitle="", yrange=None,
           rangemode="tozero"):
    """Build a {traces, layout} panel dict for an independent chart div."""
    layout = {
        "xaxis": {"type": "date"},
        "yaxis": {"title": ytitle, "rangemode": rangemode},
        "hovermode": "x unified",
        "height": height,
        "margin": {"t": 30, "b": 30, "l": 60, "r": 20},
        "legend": {"orientation": "h", "y": 1.12, "font": {"size": 11}},
        "title": {"text": title, "font": {"size": 13}, "x": 0.01,
                  "xanchor": "left", "y": 0.97},
    }
    if yrange:
        layout["yaxis"]["range"] = yrange
    return {"traces": traces, "layout": layout}


def _human_bytes(n: int) -> str:
    n = max(0, int(n))
    if n >= 1024**3:
        return f"{n / 1024**3:.1f} GB"
    if n >= 1024**2:
        return f"{n / 1024**2:.1f} MB"
    if n >= 1024:
        return f"{n / 1024:.1f} KB"
    return f"{n} B"


# ---------------------------------------------------------------------------
# Known port → service mappings
# ---------------------------------------------------------------------------

_PORT_MAP = {
    22: "SSH", 53: "DNS", 80: "HTTP", 443: "HTTPS",
    853: "DNS-over-TLS", 993: "IMAPS", 5228: "Google Push",
    5353: "mDNS",
}

# Ranges checked first (most specific → least specific).
_PORT_RANGES = [
    ((19302, 19309), "Google Meet"),
    ((8801, 8810), "Zoom"),
    ((16384, 16399), "FaceTime/RTP"),
    ((3478, 3481), "STUN/TURN"),
    ((50000, 50100), "Discord Voice"),
]


def _port_service(port_str: str) -> str:
    """Return known service name for a port number, or empty string."""
    try:
        port = int(port_str)
    except (ValueError, TypeError):
        return ""
    for (lo, hi), name in _PORT_RANGES:
        if lo <= port <= hi:
            return name
    return _PORT_MAP.get(port, "")


def _port_label(port_str: str) -> str:
    """Display label: grouped service name for ranges, :port (svc) otherwise."""
    try:
        port = int(port_str)
    except (ValueError, TypeError):
        return f":{port_str}"
    for (lo, hi), name in _PORT_RANGES:
        if lo <= port <= hi:
            return name
    svc = _PORT_MAP.get(port, "")
    if svc:
        return f":{port} ({svc})"
    return f":{port}"


def _aggregate_traffic(rows: List[Dict[str, str]], top_n: int = 10,
                       ) -> List[dict]:
    """Aggregate per-process traffic: totals + time series."""
    from collections import defaultdict
    totals: Dict[str, List[int]] = defaultdict(lambda: [0, 0, 0, 0, 0])
    series: Dict[str, List[dict]] = defaultdict(list)
    for r in rows:
        proc = tag_vpn(r.get("process", "?"))
        b_in = int(r.get("bytes_in", 0) or 0)
        b_out = int(r.get("bytes_out", 0) or 0)
        p_in = int(r.get("packets_in", 0) or 0)
        p_out = int(r.get("packets_out", 0) or 0)
        retx = int(r.get("retransmits", 0) or 0)
        totals[proc][0] += b_in
        totals[proc][1] += b_out
        totals[proc][2] += p_in
        totals[proc][3] += p_out
        totals[proc][4] += retx
        series[proc].append({
            "ts": r.get("sample_ts", ""),
            "in": b_in, "out": b_out,
        })
    ranked = sorted(totals.items(), key=lambda kv: kv[1][0] + kv[1][1],
                    reverse=True)[:top_n]
    result = []
    for proc, (total_in, total_out, total_pin, total_pout, total_retx) in ranked:
        if total_in + total_out == 0:
            continue
        total_pkts = total_pin + total_pout
        pts = series[proc]
        result.append({
            "process": proc,
            "bytes_in": total_in,
            "bytes_out": total_out,
            "packets": total_pkts,
            "retransmits": total_retx,
            "retx_pct": round(total_retx / (total_pout + total_retx) * 100, 2) if (total_pout + total_retx) else 0,
            "human_in": _human_bytes(total_in),
            "human_out": _human_bytes(total_out),
            "series_ts": [p["ts"] for p in pts],
            "series_in": [p["in"] for p in pts],
            "series_out": [p["out"] for p in pts],
        })
    return result


def _aggregate_connections(rows: List[Dict[str, str]], top_n: int = 10,
                           ) -> List[dict]:
    """Aggregate per-connection traffic: totals + time series."""
    from collections import defaultdict
    totals: Dict[str, List[int]] = defaultdict(lambda: [0, 0, 0, 0, 0])
    series: Dict[str, List[dict]] = defaultdict(list)
    for r in rows:
        proc = tag_vpn(r.get("process", "?"))
        remote = r.get("remote_ip", "?")
        port = r.get("remote_port", "?")
        key = f"{proc} \u2192 {remote}:{port}"
        b_in = int(r.get("bytes_in", 0) or 0)
        b_out = int(r.get("bytes_out", 0) or 0)
        p_in = int(r.get("packets_in", 0) or 0)
        p_out = int(r.get("packets_out", 0) or 0)
        retx = int(r.get("retransmits", 0) or 0)
        totals[key][0] += b_in
        totals[key][1] += b_out
        totals[key][2] += p_in
        totals[key][3] += p_out
        totals[key][4] += retx
        series[key].append({
            "ts": r.get("sample_ts", ""),
            "in": b_in, "out": b_out,
        })
    ranked = sorted(totals.items(), key=lambda kv: kv[1][0] + kv[1][1],
                    reverse=True)[:top_n]
    result = []
    for key, (total_in, total_out, total_pin, total_pout, total_retx) in ranked:
        if total_in + total_out == 0:
            continue
        total_pkts = total_pin + total_pout
        pts = series[key]
        result.append({
            "connection": key,
            "bytes_in": total_in,
            "bytes_out": total_out,
            "packets": total_pkts,
            "retransmits": total_retx,
            "retx_pct": round(total_retx / (total_pout + total_retx) * 100, 2) if (total_pout + total_retx) else 0,
            "human_in": _human_bytes(total_in),
            "human_out": _human_bytes(total_out),
            "series_ts": [p["ts"] for p in pts],
            "series_in": [p["in"] for p in pts],
            "series_out": [p["out"] for p in pts],
        })
    return result


def _aggregate_by_port(rows: List[Dict[str, str]], top_n: int = 15,
                       ) -> List[dict]:
    """Aggregate connection traffic by port/service with time series."""
    from collections import defaultdict
    totals: Dict[str, List[int]] = defaultdict(lambda: [0, 0, 0, 0, 0])
    series: Dict[str, List[dict]] = defaultdict(list)
    for r in rows:
        port = r.get("remote_port", "?")
        label = _port_label(port)
        b_in = int(r.get("bytes_in", 0) or 0)
        b_out = int(r.get("bytes_out", 0) or 0)
        p_in = int(r.get("packets_in", 0) or 0)
        p_out = int(r.get("packets_out", 0) or 0)
        retx = int(r.get("retransmits", 0) or 0)
        totals[label][0] += b_in
        totals[label][1] += b_out
        totals[label][2] += p_in
        totals[label][3] += p_out
        totals[label][4] += retx
        series[label].append({
            "ts": r.get("sample_ts", ""),
            "in": b_in, "out": b_out,
        })
    ranked = sorted(totals.items(), key=lambda kv: kv[1][0] + kv[1][1],
                    reverse=True)[:top_n]
    result = []
    for label, (total_in, total_out, total_pin, total_pout, total_retx) in ranked:
        if total_in + total_out == 0:
            continue
        total_pkts = total_pin + total_pout
        pts = series[label]
        result.append({
            "port": label,
            "bytes_in": total_in,
            "bytes_out": total_out,
            "packets": total_pkts,
            "retransmits": total_retx,
            "retx_pct": round(total_retx / (total_pout + total_retx) * 100, 2) if (total_pout + total_retx) else 0,
            "human_in": _human_bytes(total_in),
            "human_out": _human_bytes(total_out),
            "series_ts": [p["ts"] for p in pts],
            "series_in": [p["in"] for p in pts],
            "series_out": [p["out"] for p in pts],
        })
    return result


def build_chart_data(diag_rows: List[Dict[str, str]],
                     main_rows: List[Dict[str, str]],
                     session_name: str,
                     traffic_rows: Optional[List[Dict[str, str]]] = None,
                     conn_rows: Optional[List[Dict[str, str]]] = None,
                     udp_rows: Optional[List[Dict[str, str]]] = None,
                     ) -> dict:
    """Build JSON-serializable chart data from CSV rows."""

    # --- Diagnostics scatter ---
    diag_traces = []
    for sev in ("bad", "warn", "info", "resolved"):
        filtered = [r for r in diag_rows if r.get("severity") == sev]
        if not filtered:
            continue
        xs = [r["timestamp"] for r in filtered]
        ys = [SEVERITY_Y[sev]] * len(filtered)
        texts = [html.escape(r.get("message", "")) for r in filtered]
        hover = [f"<b>{sev.upper()}</b><br>{t}<br>{x}"
                 for t, x in zip(texts, xs)]
        diag_traces.append({
            "x": xs, "y": ys,
            "mode": "markers", "type": "scatter",
            "name": sev,
            "marker": {
                "color": SEVERITY_COLORS[sev],
                "symbol": SEVERITY_SYMBOLS[sev],
                "size": 14 if sev == "bad" else 11,
                "line": {"width": 1, "color": "#333"},
            },
            "text": hover,
            "hoverinfo": "text",
        })

    diag_layout = {
        "title": {"text": f"Diagnostics \u2014 {session_name}",
                  "font": {"size": 14}, "x": 0.01, "xanchor": "left"},
        "xaxis": {"type": "date"},
        "yaxis": {
            "title": "",
            "tickvals": [0, 1, 2, 3],
            "ticktext": ["resolved", "info", "warn", "bad"],
            "range": [-0.5, 3.5],
        },
        "hovermode": "closest",
        "height": 250,
        "margin": {"t": 35, "b": 30, "l": 70, "r": 20},
        "legend": {"orientation": "h", "y": 1.12},
    }

    # --- Metric panels (each is a separate chart div) ---
    panels = []
    if main_rows:
        ts = [r.get("timestamp", "") for r in main_rows]

        def ex(field):
            return [to_float(r.get(field, "")) for r in main_rows]

        panels.append(_panel("Latency", [
            _make_line_trace(ts, ex("ping_avg_ms"), "Ping", "ms", "#e74c3c"),
            _make_line_trace(ts, ex("gw_ping_ms"), "Gateway", "ms", "#e67e22"),
            _make_line_trace(ts, ex("jitter_ms"), "Jitter", "ms", "#9b59b6"),
        ], ytitle="ms"))

        panels.append(_panel("DNS", [
            _make_line_trace(ts, ex("dns_ms"), "DNS", "ms", "#1abc9c",
                             fill="tozeroy"),
        ], height=150, ytitle="ms"))

        panels.append(_panel("Packet Loss", [
            _make_line_trace(ts, ex("loss_%"), "Loss", "%", "#e74c3c",
                             fill="tozeroy"),
        ], height=150, ytitle="%"))

        panels.append(_panel("WiFi Signal", [
            _make_line_trace(ts, ex("rssi_dBm"), "RSSI", "dBm", "#3498db"),
            _make_line_trace(ts, ex("noise_dBm"), "Noise", "dBm", "#e74c3c"),
        ], ytitle="dBm", rangemode="normal"))

        panels.append(_panel("SNR", [
            _make_line_trace(ts, ex("snr_dB"), "SNR", "dB", "#f39c12",
                             fill="tozeroy"),
        ], height=150, ytitle="dB"))

        panels.append(_panel("TX Rate", [
            _make_line_trace(ts, ex("tx_rate_Mbps"), "TX Rate", "Mbps",
                             "#2ecc71", fill="tozeroy"),
        ], height=170, ytitle="Mbps"))

        panels.append(_panel("MCS Index", [
            _make_line_trace(ts, ex("mcs"), "MCS", "", "#e67e22"),
        ], height=150, ytitle="MCS", yrange=[-0.5, 15.5]))

        panels.append(_panel("System", [
            _make_line_trace(ts, ex("cpu_usage"), "CPU", "%", "#e74c3c"),
            _make_line_trace(ts, ex("cca_pct"), "CCA", "%", "#9b59b6"),
        ], height=170, ytitle="%"))

    return {
        "diagTraces": diag_traces,
        "diagLayout": diag_layout,
        "panels": panels,
        "hasMetrics": bool(main_rows),
        "tcpTraffic": _aggregate_traffic(traffic_rows or []),
        "udpTraffic": _aggregate_traffic(udp_rows or []),
        "connections": _aggregate_connections(conn_rows or []),
        "portTraffic": _aggregate_by_port(conn_rows or []),
    }


def build_html(diag_rows: List[Dict[str, str]],
               main_rows: List[Dict[str, str]],
               session_name: str,
               live: bool = False,
               traffic_rows: Optional[List[Dict[str, str]]] = None,
               conn_rows: Optional[List[Dict[str, str]]] = None,
               udp_rows: Optional[List[Dict[str, str]]] = None,
               ) -> str:
    """Build a self-contained HTML string with Plotly charts."""
    data = build_chart_data(diag_rows, main_rows, session_name,
                            traffic_rows=traffic_rows, conn_rows=conn_rows,
                            udp_rows=udp_rows)
    initial_data_json = json.dumps(data)
    escaped_session = html.escape(session_name)

    # Auto-refresh JS — only included in live mode
    if live:
        refresh_js = """
var autoRefresh = true;
var refreshInterval = 10;
var refreshTimer = null;

function updateRefreshStatus() {
  var el = document.getElementById('refresh-status');
  if (autoRefresh) {
    el.textContent = 'Auto-refresh: ON (' + refreshInterval + 's)';
    el.style.color = '#2ecc71';
  } else {
    el.textContent = 'Auto-refresh: OFF';
    el.style.color = '#888';
  }
}

function fetchAndUpdate() {
  fetch('/api/data')
    .then(function(r) { return r.json(); })
    .then(function(data) {
      renderAll(data);
      document.getElementById('last-update').textContent =
        'Last update: ' + new Date().toLocaleTimeString();
    })
    .catch(function(err) {
      document.getElementById('last-update').textContent =
        'Update failed: ' + err.message;
    });
}

function scheduleRefresh() {
  if (refreshTimer) clearInterval(refreshTimer);
  if (autoRefresh) {
    refreshTimer = setInterval(fetchAndUpdate, refreshInterval * 1000);
  }
}

document.getElementById('auto-refresh-toggle').addEventListener('change', function(e) {
  autoRefresh = e.target.value === 'on';
  scheduleRefresh();
  updateRefreshStatus();
});

document.getElementById('refresh-interval').addEventListener('change', function(e) {
  refreshInterval = parseInt(e.target.value, 10);
  scheduleRefresh();
  updateRefreshStatus();
});

document.getElementById('refresh-now').addEventListener('click', function() {
  fetchAndUpdate();
});

updateRefreshStatus();
scheduleRefresh();
"""
        refresh_controls = """
<div class="controls">
  <label>
    <select id="auto-refresh-toggle">
      <option value="on" selected>Auto-refresh ON</option>
      <option value="off">Auto-refresh OFF</option>
    </select>
  </label>
  <label>
    Interval:
    <select id="refresh-interval">
      <option value="5">5s</option>
      <option value="10" selected>10s</option>
      <option value="30">30s</option>
      <option value="60">60s</option>
    </select>
  </label>
  <button id="refresh-now">Refresh now</button>
  <span id="refresh-status"></span>
  <span id="last-update" style="margin-left:15px;color:#666;font-size:0.85em;"></span>
</div>
"""
    else:
        refresh_js = ""
        refresh_controls = ""

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>netmon \u2014 {escaped_session}</title>
<script src="https://cdn.plot.ly/plotly-2.35.2.min.js"></script>
<style>
  * {{ box-sizing: border-box; }}
  body {{ font-family: -apple-system, BlinkMacSystemFont, sans-serif;
         margin: 0; padding: 20px; background: #1a1a2e; color: #eee; }}
  h1 {{ font-size: 1.3em; margin: 0 0 15px 0; color: #e0e0e0; }}
  .panel {{ background: #16213e; border-radius: 8px; margin-bottom: 12px;
            padding: 4px 8px; }}
  .stats {{ display: flex; gap: 15px; margin-bottom: 16px; flex-wrap: wrap; }}
  .stat-card {{ background: #16213e; border-radius: 8px; padding: 10px 18px;
                min-width: 100px; }}
  .stat-card .label {{ font-size: 0.8em; color: #888; }}
  .stat-card .value {{ font-size: 1.4em; font-weight: 600; }}
  .bad {{ color: #e74c3c; }}
  .warn {{ color: #f39c12; }}
  .info {{ color: #3498db; }}
  .resolved {{ color: #2ecc71; }}
  .controls {{ display: flex; align-items: center; gap: 12px;
               margin-bottom: 16px; flex-wrap: wrap; }}
  .controls select, .controls button {{
    background: #16213e; color: #eee; border: 1px solid #2a4080;
    border-radius: 4px; padding: 5px 10px; font-size: 0.9em; cursor: pointer;
  }}
  .controls button:hover {{ background: #1a4080; }}
  #panels-container .panel {{ margin-bottom: 8px; }}
  .traffic-section {{ margin-top: 20px; }}
  .traffic-section h2 {{ font-size: 1.1em; color: #bbb; margin: 18px 0 8px 0; }}
  .traffic-table {{ width: 100%; border-collapse: collapse; background: #16213e;
                    border-radius: 8px; margin-bottom: 12px; }}
  .traffic-table th {{ text-align: left; padding: 8px 12px; color: #888;
                       font-size: 0.8em; border-bottom: 1px solid #1a4080;
                       text-transform: uppercase; letter-spacing: 0.5px; }}
  .traffic-table td {{ padding: 6px 12px; border-bottom: 1px solid #0f3460;
                       font-size: 0.9em; position: relative; }}
  .traffic-table tr:last-child td {{ border-bottom: none; }}
  .traffic-table .name {{ color: #eee; max-width: 250px; overflow: hidden;
                          text-overflow: ellipsis; white-space: nowrap; }}
  .traffic-table .bytes {{ color: #3498db; text-align: right; font-family: monospace; }}
  .traffic-table .retx {{ color: #e74c3c; text-align: right; font-family: monospace; }}
  .traffic-table .spark {{ width: 220px; height: 60px; }}
  #spark-tooltip {{ position: fixed; z-index: 10000; pointer-events: none;
                    background: #222; border: 1px solid #555; border-radius: 4px;
                    padding: 6px 10px; font-size: 13px; color: #eee;
                    display: none; white-space: nowrap;
                    box-shadow: 0 2px 8px rgba(0,0,0,0.5); }}
</style>
</head>
<body>
<h1>netmon diagnostics \u2014 {escaped_session}</h1>
{refresh_controls}
<div class="stats" id="stats"></div>
<div class="panel" id="diag-chart"></div>
<div id="panels-container"></div>
<div class="traffic-section">
<div class="controls" style="margin-top:20px">
  <label>Aggregate traffic:
    <select id="traffic-agg">
      <option value="0">No aggregation</option>
      <option value="30" selected>30s</option>
      <option value="60">1m</option>
      <option value="300">5m</option>
      <option value="600">10m</option>
      <option value="900">15m</option>
      <option value="1800">30m</option>
      <option value="3600">1h</option>
    </select>
  </label>
</div>
<div id="traffic-section"></div>
</div>
<div id="spark-tooltip"></div>

<script>
var initialData = {initial_data_json};

var dark = {{
  paper_bgcolor: '#16213e',
  plot_bgcolor: '#0f3460',
  font: {{ color: '#ccc', size: 11 }},
  xaxis: {{ gridcolor: '#1a4080', linecolor: '#1a4080' }},
  yaxis: {{ gridcolor: '#1a4080', linecolor: '#1a4080' }},
}};

var plotCfg = {{ responsive: true, displayModeBar: false }};

function updateStats(diagTraces) {{
  var counts = {{ bad: 0, warn: 0, info: 0, resolved: 0 }};
  diagTraces.forEach(function(t) {{ counts[t.name] = t.x.length; }});
  var h = '';
  [['bad','Errors'],['warn','Warnings'],['info','Info'],['resolved','Resolved']].forEach(function(p) {{
    h += '<div class="stat-card"><div class="label">' + p[1] + '</div>'
       + '<div class="value ' + p[0] + '">' + counts[p[0]] + '</div></div>';
  }});
  document.getElementById('stats').innerHTML = h;
}}

function renderPanels(panels) {{
  var container = document.getElementById('panels-container');
  // Create divs on first render
  while (container.children.length < panels.length) {{
    var div = document.createElement('div');
    div.className = 'panel';
    div.id = 'panel-' + container.children.length;
    container.appendChild(div);
  }}
  panels.forEach(function(panel, i) {{
    var id = 'panel-' + i;
    var layout = Object.assign({{}}, panel.layout, dark);
    Plotly.react(id, panel.traces, layout, plotCfg);
  }});
}}

function renderTrafficTable(containerId, title, items, nameField) {{
  if (!items || items.length === 0) return '';
  var h = '<h2>' + title + '</h2>';
  h += '<table class="traffic-table"><thead><tr>';
  h += '<th>' + (nameField === 'connection' ? 'Connection' : nameField === 'port' ? 'Port / Service' : 'Process') + '</th>';
  h += '<th style="text-align:right">In</th><th style="text-align:right">Out</th>';
  h += '<th style="text-align:right">Retx</th>';
  h += '<th>Traffic over time</th></tr></thead><tbody>';
  items.forEach(function(item, i) {{
    var id = containerId + '-spark-' + i;
    var retx = item.retransmits || 0;
    var pct = item.retx_pct || 0;
    var retxColor = pct > 2 ? '#e74c3c' : pct > 0.5 ? '#f39c12' : '#888';
    var retxStr = retx === 0 ? '<span style="color:#666">-</span>'
      : '<span style="color:' + retxColor + '">' + retx + ' (' + pct.toFixed(1) + '%)</span>';
    h += '<tr><td class="name" title="' + item[nameField] + '">' + item[nameField] + '</td>';
    h += '<td class="bytes">' + item.human_in + '</td>';
    h += '<td class="bytes">' + item.human_out + '</td>';
    h += '<td class="retx">' + retxStr + '</td>';
    h += '<td class="spark"><div id="' + id + '"></div></td></tr>';
  }});
  h += '</tbody></table>';
  return h;
}}

function humanBytes(n) {{
  if (n >= 1073741824) return (n / 1073741824).toFixed(1) + ' GB';
  if (n >= 1048576) return (n / 1048576).toFixed(1) + ' MB';
  if (n >= 1024) return (n / 1024).toFixed(1) + ' KB';
  return n + ' B';
}}

function getAggBucket() {{
  var el = document.getElementById('traffic-agg');
  return el ? parseInt(el.value, 10) : 0;
}}

function aggregateSeries(timestamps, valIn, valOut, bucketSec) {{
  if (!bucketSec || bucketSec <= 0 || timestamps.length === 0) {{
    return {{ ts: timestamps, sin: valIn, sout: valOut }};
  }}
  var buckets = {{}};
  var order = [];
  for (var i = 0; i < timestamps.length; i++) {{
    var t = new Date(timestamps[i]).getTime();
    if (isNaN(t)) continue;
    var key = Math.floor(t / (bucketSec * 1000)) * (bucketSec * 1000);
    if (!(key in buckets)) {{
      buckets[key] = [0, 0];
      order.push(key);
    }}
    buckets[key][0] += valIn[i];
    buckets[key][1] += valOut[i];
  }}
  var rts = [], rin = [], rout = [];
  order.forEach(function(k) {{
    rts.push(new Date(k).toISOString().replace('T', ' ').substring(0, 19));
    rin.push(buckets[k][0]);
    rout.push(buckets[k][1]);
  }});
  return {{ ts: rts, sin: rin, sout: rout }};
}}

function attachSparkTooltip(chartId, hIn, hOut) {{
  var tip = document.getElementById('spark-tooltip');
  var chartEl = document.getElementById(chartId);
  chartEl.on('plotly_hover', function(ev) {{
    var pt = ev.points[0];
    var idx = pt.pointIndex;
    var ts = pt.x;
    var tsShort = typeof ts === 'string' ? ts.split(' ').pop() || ts : ts;
    tip.innerHTML = '<b>' + tsShort + '</b><br>'
      + '<span style="color:#3498db">\u25cf</span> ' + hIn[idx]
      + '<br><span style="color:#2ecc71">\u25cf</span> ' + hOut[idx];
    tip.style.display = 'block';
  }});
  chartEl.on('plotly_unhover', function() {{
    tip.style.display = 'none';
  }});
  chartEl.addEventListener('mousemove', function(e) {{
    var tw = tip.offsetWidth || 120;
    var th = tip.offsetHeight || 60;
    var left = e.clientX + 12;
    var top = e.clientY - 10;
    if (left + tw > window.innerWidth - 4) left = e.clientX - tw - 12;
    if (top + th > window.innerHeight - 4) top = e.clientY - th - 4;
    tip.style.left = left + 'px';
    tip.style.top = top + 'px';
  }});
}}

function renderTrafficSparklines(containerId, items) {{
  if (!items) return;
  var bucket = getAggBucket();
  items.forEach(function(item, i) {{
    var id = containerId + '-spark-' + i;
    var el = document.getElementById(id);
    if (!el || !item.series_ts || item.series_ts.length < 2) return;
    var agg = aggregateSeries(item.series_ts, item.series_in, item.series_out, bucket);
    var hoverIn = agg.sin.map(function(v) {{ return 'In: ' + humanBytes(v); }});
    var hoverOut = agg.sout.map(function(v) {{ return 'Out: ' + humanBytes(v); }});
    Plotly.newPlot(id, [
      {{ x: agg.ts, y: agg.sin, mode: 'lines', name: 'In',
        line: {{ color: '#3498db', width: 1.5 }},
        text: hoverIn, hoverinfo: 'none' }},
      {{ x: agg.ts, y: agg.sout, mode: 'lines', name: 'Out',
        line: {{ color: '#2ecc71', width: 1.5 }},
        text: hoverOut, hoverinfo: 'none' }}
    ], {{
      paper_bgcolor: 'rgba(0,0,0,0)', plot_bgcolor: 'rgba(0,0,0,0)',
      margin: {{ t: 2, b: 2, l: 0, r: 0 }},
      xaxis: {{ visible: false }}, yaxis: {{ visible: false, rangemode: 'tozero' }},
      showlegend: false, height: 60, hovermode: 'closest',
    }}, {{ responsive: true, displayModeBar: false }});
    attachSparkTooltip(id, hoverIn, hoverOut);
  }});
}}

var _lastTrafficData = null;

function renderTraffic(data) {{
  _lastTrafficData = data;
  var sec = document.getElementById('traffic-section');
  if (!sec) return;
  var h = '';
  h += renderTrafficTable('tcp', 'TCP Traffic by Process', data.tcpTraffic, 'process');
  h += renderTrafficTable('udp', 'UDP Traffic by Process', data.udpTraffic, 'process');
  h += renderTrafficTable('port', 'Traffic by Port / Service', data.portTraffic, 'port');
  h += renderTrafficTable('conn', 'Top Connections', data.connections, 'connection');
  sec.innerHTML = h;
  renderTrafficSparklines('tcp', data.tcpTraffic);
  renderTrafficSparklines('udp', data.udpTraffic);
  renderTrafficSparklines('port', data.portTraffic);
  renderTrafficSparklines('conn', data.connections);
}}

(function() {{
  var aggEl = document.getElementById('traffic-agg');
  if (aggEl) aggEl.addEventListener('change', function() {{
    if (_lastTrafficData) renderTraffic(_lastTrafficData);
  }});
}})()

function renderAll(data) {{
  var diagLayout = Object.assign({{}}, data.diagLayout, dark);
  Plotly.react('diag-chart', data.diagTraces, diagLayout, plotCfg);
  updateStats(data.diagTraces);
  if (data.panels && data.panels.length > 0) {{
    renderPanels(data.panels);
  }}
  renderTraffic(data);
}}

renderAll(initialData);

{refresh_js}
</script>
</body>
</html>"""


# ---------------------------------------------------------------------------
# HTTP server for live mode
# ---------------------------------------------------------------------------

class ChartHandler(BaseHTTPRequestHandler):
    """Serves the chart HTML and a JSON data API."""

    def __init__(self, *args, diag_file: Path, main_file: Optional[Path],
                 traffic_file: Path, conn_file: Path, udp_file: Path,
                 session_name: str, **kwargs):
        self.diag_file = diag_file
        self.main_file = main_file
        self.traffic_file = traffic_file
        self.conn_file = conn_file
        self.udp_file = udp_file
        self.session_name = session_name
        super().__init__(*args, **kwargs)

    def do_GET(self):
        if self.path == "/":
            self._serve_html()
        elif self.path == "/api/data":
            self._serve_data()
        else:
            self.send_error(404)

    def _read_session(self):
        def _read(p):
            return read_csv_rows(p) if p and p.exists() else []
        return (_read(self.diag_file), _read(self.main_file),
                _read(self.traffic_file), _read(self.conn_file),
                _read(self.udp_file))

    def _serve_html(self):
        diag, main, tcp, conn, udp = self._read_session()
        content = build_html(diag, main, self.session_name, live=True,
                             traffic_rows=tcp, conn_rows=conn, udp_rows=udp)
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        self.wfile.write(content.encode("utf-8"))

    def _serve_data(self):
        diag, main, tcp, conn, udp = self._read_session()
        data = build_chart_data(diag, main, self.session_name,
                                traffic_rows=tcp, conn_rows=conn, udp_rows=udp)
        payload = json.dumps(data).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, format, *args):
        # Silence per-request logs
        pass


def run_server(diag_file: Path, main_file: Optional[Path],
               traffic_file: Path, conn_file: Path, udp_file: Path,
               session_name: str, port: int, no_open: bool) -> int:
    handler = partial(ChartHandler, diag_file=diag_file,
                      main_file=main_file, traffic_file=traffic_file,
                      conn_file=conn_file, udp_file=udp_file,
                      session_name=session_name)
    server = HTTPServer(("127.0.0.1", port), handler)
    actual_port = server.server_address[1]
    url = f"http://127.0.0.1:{actual_port}"

    print(f"Serving chart at {url}")
    print("Press Ctrl+C to stop.")

    if not no_open:
        threading.Timer(0.3, lambda: webbrowser.open(url)).start()

    def _shutdown(sig, frame):
        print("\nShutting down server.")
        threading.Thread(target=server.shutdown, daemon=True).start()

    signal.signal(signal.SIGINT, _shutdown)
    signal.signal(signal.SIGTERM, _shutdown)

    server.serve_forever()
    return 0


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> int:
    args = parse_args()

    # Resolve files
    diag_file: Optional[Path] = None
    main_file: Optional[Path] = None

    if args.diag_file:
        diag_file = Path(args.diag_file)
        main_file = resolve_main_file(diag_file)
    elif args.main_file:
        main_file = Path(args.main_file)
        diag_file = resolve_diag_file(main_file)
    else:
        main_file = latest_main_log(Path(args.log_dir))
        if main_file is None:
            print(f"No session logs found in {args.log_dir}", file=sys.stderr)
            return 1
        diag_file = resolve_diag_file(main_file)

    session_name = derive_session_name(main_file)

    # Resolve related session files
    if main_file:
        traffic_file, conn_file, _scan, udp_file, _ = resolve_related(main_file)
    else:
        traffic_file = conn_file = udp_file = Path("/dev/null")

    diag_rows = read_csv_rows(diag_file) if diag_file.exists() else []

    def _read(p):
        return read_csv_rows(p) if p.exists() else []

    # Static export mode
    if args.output:
        main_rows = _read(main_file) if main_file else []
        html_content = build_html(
            diag_rows, main_rows, session_name, live=False,
            traffic_rows=_read(traffic_file), conn_rows=_read(conn_file),
            udp_rows=_read(udp_file))
        out_path = Path(args.output)
        out_path.write_text(html_content)
        print(f"Chart written to: {out_path}")
        if not args.no_open:
            webbrowser.open(f"file://{out_path.resolve()}")
        return 0

    # Live server mode (default)
    return run_server(diag_file, main_file, traffic_file, conn_file,
                      udp_file, session_name, args.port, args.no_open)


if __name__ == "__main__":
    sys.exit(main())
