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
import csv
import html
import json
import os
import signal
import sys
import tempfile
import threading
import webbrowser
from functools import partial
from http.server import HTTPServer, BaseHTTPRequestHandler
from pathlib import Path
from typing import Dict, List, Optional, Tuple


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


def _is_session_dir(path: Path) -> bool:
    return path.name == "main.csv"


def resolve_diag_file(main_file: Path) -> Path:
    if _is_session_dir(main_file):
        return main_file.parent / "diagnostics.csv"
    stem = str(main_file)
    base = stem[:-4] if stem.endswith(".csv") else stem
    return Path(f"{base}-diagnostics.csv")


def resolve_main_file(diag_file: Path) -> Path:
    if diag_file.name == "diagnostics.csv":
        return diag_file.parent / "main.csv"
    stem = str(diag_file)
    base = stem.replace("-diagnostics.csv", ".csv")
    return Path(base)


def to_float(value: str) -> Optional[float]:
    value = value.strip()
    if not value or value == "?":
        return None
    try:
        return float(value)
    except ValueError:
        return None


def read_diag_csv(path: Path) -> List[Dict[str, str]]:
    if not path.exists():
        return []
    rows = []
    with open(path, newline="") as f:
        reader = csv.DictReader(f)
        for row in reader:
            rows.append(row)
    return rows


def read_main_csv(path: Path) -> List[Dict[str, str]]:
    if not path.exists():
        return []
    rows = []
    with open(path, newline="") as f:
        reader = csv.DictReader(f)
        for row in reader:
            rows.append(row)
    return rows


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


def build_chart_data(diag_rows: List[Dict[str, str]],
                     main_rows: List[Dict[str, str]],
                     session_name: str) -> dict:
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
    }


def build_html(diag_rows: List[Dict[str, str]],
               main_rows: List[Dict[str, str]],
               session_name: str,
               live: bool = False) -> str:
    """Build a self-contained HTML string with Plotly charts."""
    data = build_chart_data(diag_rows, main_rows, session_name)
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
</style>
</head>
<body>
<h1>netmon diagnostics \u2014 {escaped_session}</h1>
{refresh_controls}
<div class="stats" id="stats"></div>
<div class="panel" id="diag-chart"></div>
<div id="panels-container"></div>

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

function renderAll(data) {{
  var diagLayout = Object.assign({{}}, data.diagLayout, dark);
  Plotly.react('diag-chart', data.diagTraces, diagLayout, plotCfg);
  updateStats(data.diagTraces);
  if (data.panels && data.panels.length > 0) {{
    renderPanels(data.panels);
  }}
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
                 session_name: str, **kwargs):
        self.diag_file = diag_file
        self.main_file = main_file
        self.session_name = session_name
        super().__init__(*args, **kwargs)

    def do_GET(self):
        if self.path == "/":
            self._serve_html()
        elif self.path == "/api/data":
            self._serve_data()
        else:
            self.send_error(404)

    def _serve_html(self):
        diag_rows = read_diag_csv(self.diag_file)
        main_rows = (read_main_csv(self.main_file)
                     if self.main_file and self.main_file.exists() else [])
        content = build_html(diag_rows, main_rows, self.session_name, live=True)
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        self.wfile.write(content.encode("utf-8"))

    def _serve_data(self):
        diag_rows = read_diag_csv(self.diag_file)
        main_rows = (read_main_csv(self.main_file)
                     if self.main_file and self.main_file.exists() else [])
        data = build_chart_data(diag_rows, main_rows, self.session_name)
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
               session_name: str, port: int, no_open: bool) -> int:
    handler = partial(ChartHandler, diag_file=diag_file,
                      main_file=main_file, session_name=session_name)
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

    if main_file and _is_session_dir(main_file):
        session_name = main_file.parent.name
    elif main_file:
        session_name = main_file.stem
    else:
        session_name = "netmon"

    diag_rows = read_diag_csv(diag_file) if diag_file.exists() else []

    # Static export mode
    if args.output:
        main_rows = (read_main_csv(main_file)
                     if main_file and main_file.exists() else [])
        html_content = build_html(diag_rows, main_rows, session_name, live=False)
        out_path = Path(args.output)
        out_path.write_text(html_content)
        print(f"Chart written to: {out_path}")
        if not args.no_open:
            webbrowser.open(f"file://{out_path.resolve()}")
        return 0

    # Live server mode (default)
    return run_server(diag_file, main_file, session_name,
                      args.port, args.no_open)


if __name__ == "__main__":
    sys.exit(main())
