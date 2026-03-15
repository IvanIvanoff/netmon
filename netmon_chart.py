#!/usr/bin/env python3
"""
netmon_chart.py — Generate an interactive HTML timeline of diagnostics.

Reads a netmon diagnostics CSV (call-*-diagnostics.csv) and the corresponding
main CSV to produce a self-contained HTML file with:
  - A scatter timeline of diagnostic events (hover for details)
  - Metric overlays (ping, loss, RSSI) for context

Usage:
    python3 netmon_chart.py [--main-file call-XXXX.csv | --log-dir ~/call-network-logs]
    python3 netmon_chart.py --diag-file call-XXXX-diagnostics.csv

Opens the generated HTML in the default browser.
"""

from __future__ import annotations

import argparse
import csv
import html
import json
import os
import sys
import tempfile
import webbrowser
from pathlib import Path
from typing import Dict, List, Optional, Tuple


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Interactive netmon diagnostics chart")
    p.add_argument("--diag-file", help="Path to diagnostics CSV directly")
    p.add_argument("--main-file", help="Path to main call-*.csv (auto-resolves diagnostics)")
    p.add_argument("--log-dir", default=str(Path.home() / "call-network-logs"),
                   help="Log directory (uses latest session)")
    p.add_argument("-o", "--output", help="Output HTML path (default: open temp file)")
    p.add_argument("--no-open", action="store_true", help="Don't open browser")
    return p.parse_args()


def latest_main_log(log_dir: Path) -> Optional[Path]:
    candidates = []
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


def resolve_diag_file(main_file: Path) -> Path:
    stem = str(main_file)
    base = stem[:-4] if stem.endswith(".csv") else stem
    return Path(f"{base}-diagnostics.csv")


def resolve_main_file(diag_file: Path) -> Path:
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


def build_html(diag_rows: List[Dict[str, str]],
               main_rows: List[Dict[str, str]],
               session_name: str) -> str:
    """Build a self-contained HTML string with Plotly charts."""

    # --- Diagnostic events trace ---
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
            "x": xs,
            "y": ys,
            "mode": "markers",
            "type": "scatter",
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
        "title": f"Diagnostics Timeline — {session_name}",
        "xaxis": {"title": "Time", "type": "date"},
        "yaxis": {
            "title": "",
            "tickvals": [0, 1, 2, 3],
            "ticktext": ["resolved", "info", "warn", "bad"],
            "range": [-0.5, 3.5],
        },
        "hovermode": "closest",
        "height": 300,
        "margin": {"t": 50, "b": 50, "l": 80, "r": 30},
        "legend": {"orientation": "h", "y": -0.2},
    }

    # --- Metric traces from main CSV ---
    metric_traces = []
    metric_layout = {}
    if main_rows:
        timestamps = [r.get("timestamp", "") for r in main_rows]

        def extract(field: str) -> List[Optional[float]]:
            return [to_float(r.get(field, "")) for r in main_rows]

        ping_vals = extract("ping_avg_ms")
        loss_vals = extract("loss_%")
        rssi_vals = extract("rssi_dBm")
        gw_vals = extract("gw_ping_ms")
        jitter_vals = extract("jitter_ms")

        def make_trace(vals, name, color, yaxis="y"):
            xs = []
            ys = []
            for t, v in zip(timestamps, vals):
                if v is not None:
                    xs.append(t)
                    ys.append(v)
            return {
                "x": xs, "y": ys,
                "mode": "lines",
                "type": "scatter",
                "name": name,
                "line": {"color": color, "width": 1.5},
                "yaxis": yaxis,
            }

        metric_traces = [
            make_trace(ping_vals, "Ping (ms)", "#e74c3c"),
            make_trace(gw_vals, "Gateway (ms)", "#e67e22"),
            make_trace(jitter_vals, "Jitter (ms)", "#9b59b6"),
            make_trace(loss_vals, "Loss (%)", "#2ecc71", "y2"),
            make_trace(rssi_vals, "RSSI (dBm)", "#3498db", "y3"),
        ]

        metric_layout = {
            "title": "Network Metrics",
            "xaxis": {"title": "Time", "type": "date"},
            "yaxis": {"title": "Latency (ms)", "rangemode": "tozero"},
            "yaxis2": {
                "title": "Loss %",
                "overlaying": "y",
                "side": "right",
                "rangemode": "tozero",
                "showgrid": False,
            },
            "yaxis3": {
                "title": "RSSI (dBm)",
                "overlaying": "y",
                "side": "right",
                "position": 0.95,
                "showgrid": False,
            },
            "hovermode": "x unified",
            "height": 350,
            "margin": {"t": 50, "b": 50, "l": 80, "r": 80},
            "legend": {"orientation": "h", "y": -0.25},
        }

    diag_traces_json = json.dumps(diag_traces)
    diag_layout_json = json.dumps(diag_layout)
    metric_traces_json = json.dumps(metric_traces)
    metric_layout_json = json.dumps(metric_layout)

    has_metrics = "true" if main_rows else "false"

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>netmon — {html.escape(session_name)}</title>
<script src="https://cdn.plot.ly/plotly-2.35.2.min.js"></script>
<style>
  body {{ font-family: -apple-system, BlinkMacSystemFont, sans-serif;
         margin: 0; padding: 20px; background: #1a1a2e; color: #eee; }}
  h1 {{ font-size: 1.3em; margin: 0 0 15px 0; color: #e0e0e0; }}
  .chart {{ background: #16213e; border-radius: 8px; padding: 10px;
            margin-bottom: 20px; }}
  .stats {{ display: flex; gap: 20px; margin-bottom: 20px; flex-wrap: wrap; }}
  .stat-card {{ background: #16213e; border-radius: 8px; padding: 12px 20px;
                min-width: 120px; }}
  .stat-card .label {{ font-size: 0.8em; color: #888; }}
  .stat-card .value {{ font-size: 1.4em; font-weight: 600; }}
  .bad {{ color: #e74c3c; }}
  .warn {{ color: #f39c12; }}
  .info {{ color: #3498db; }}
  .resolved {{ color: #2ecc71; }}
</style>
</head>
<body>
<h1>netmon diagnostics — {html.escape(session_name)}</h1>

<div class="stats" id="stats"></div>
<div class="chart" id="diag-chart"></div>
<div class="chart" id="metric-chart" style="display:none"></div>

<script>
var diagTraces = {diag_traces_json};
var diagLayout = {diag_layout_json};
var metricTraces = {metric_traces_json};
var metricLayout = {metric_layout_json};
var hasMetrics = {has_metrics};

// Dark theme for plotly
var darkTemplate = {{
  paper_bgcolor: '#16213e',
  plot_bgcolor: '#0f3460',
  font: {{ color: '#eee' }},
  xaxis: {{ gridcolor: '#1a4080' }},
  yaxis: {{ gridcolor: '#1a4080' }},
}};

Object.assign(diagLayout, darkTemplate);
if (hasMetrics) Object.assign(metricLayout, darkTemplate);

Plotly.newPlot('diag-chart', diagTraces, diagLayout, {{responsive: true}});

if (hasMetrics) {{
  document.getElementById('metric-chart').style.display = 'block';
  Plotly.newPlot('metric-chart', metricTraces, metricLayout, {{responsive: true}});
}}

// Summary stats
var counts = {{ bad: 0, warn: 0, info: 0, resolved: 0 }};
diagTraces.forEach(function(t) {{ counts[t.name] = t.x.length; }});
var statsHtml = '';
[['bad', 'Errors'], ['warn', 'Warnings'], ['info', 'Info'], ['resolved', 'Resolved']].forEach(function(item) {{
  var cls = item[0], label = item[1];
  statsHtml += '<div class="stat-card"><div class="label">' + label + '</div>'
    + '<div class="value ' + cls + '">' + counts[cls] + '</div></div>';
}});
document.getElementById('stats').innerHTML = statsHtml;
</script>
</body>
</html>"""


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

    if not diag_file.exists():
        print(f"No diagnostics file found: {diag_file}", file=sys.stderr)
        print("Run a monitoring session first — diagnostics are logged while the TUI is active.",
              file=sys.stderr)
        return 1

    session_name = main_file.stem if main_file else diag_file.stem

    diag_rows = read_diag_csv(diag_file)
    main_rows = read_main_csv(main_file) if main_file and main_file.exists() else []

    if not diag_rows:
        print(f"Diagnostics file is empty: {diag_file}", file=sys.stderr)
        return 1

    html_content = build_html(diag_rows, main_rows, session_name)

    if args.output:
        out_path = Path(args.output)
    else:
        fd, tmp = tempfile.mkstemp(suffix=".html", prefix="netmon-chart-")
        os.close(fd)
        out_path = Path(tmp)

    out_path.write_text(html_content)
    print(f"Chart written to: {out_path}")

    if not args.no_open:
        webbrowser.open(f"file://{out_path.resolve()}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
