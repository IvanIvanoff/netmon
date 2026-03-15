"""Tests for netmon_chart.py — chart generation."""

from pathlib import Path

import pytest

from netmon_chart import (
    read_diag_csv,
    read_main_csv,
    build_html,
    build_chart_data,
    resolve_diag_file,
    resolve_main_file,
    latest_main_log,
)

SAMPLE_MAIN_ROW = {
    "timestamp": "2025-01-15 10:00:05", "ping_avg_ms": "10",
    "loss_%": "0", "rssi_dBm": "-50", "noise_dBm": "-90",
    "snr_dB": "40", "gw_ping_ms": "5", "jitter_ms": "1",
    "dns_ms": "15", "tx_rate_Mbps": "400", "mcs": "9",
    "cpu_usage": "25", "cca_pct": "10",
}

SAMPLE_DIAG = [{"timestamp": "2025-01-15 10:00:05", "severity": "warn",
                "message": "x"}]


class TestReadDiagCsv:
    def test_reads_fixture(self, diag_csv):
        rows = read_diag_csv(diag_csv)
        assert len(rows) == 6
        assert rows[0]["severity"] == "warn"
        assert rows[1]["severity"] == "bad"
        assert rows[2]["severity"] == "resolved"

    def test_missing_file(self, tmp_path):
        rows = read_diag_csv(tmp_path / "nonexistent.csv")
        assert rows == []


class TestResolvePaths:
    def test_resolve_diag_file_new_format(self):
        main = Path("/logs/call-20250115/main.csv")
        assert resolve_diag_file(main) == Path("/logs/call-20250115/diagnostics.csv")

    def test_resolve_diag_file_old_format(self):
        main = Path("/logs/call-20250115.csv")
        assert resolve_diag_file(main) == Path("/logs/call-20250115-diagnostics.csv")

    def test_resolve_main_file_new_format(self):
        diag = Path("/logs/call-20250115/diagnostics.csv")
        assert resolve_main_file(diag) == Path("/logs/call-20250115/main.csv")

    def test_resolve_main_file_old_format(self):
        diag = Path("/logs/call-20250115-diagnostics.csv")
        assert resolve_main_file(diag) == Path("/logs/call-20250115.csv")


class TestLatestMainLog:
    def test_finds_session_dir(self, tmp_path):
        session = tmp_path / "call-20250115"
        session.mkdir()
        main = session / "main.csv"
        main.write_text("timestamp\n")
        result = latest_main_log(tmp_path)
        assert result == main

    def test_finds_old_flat_file(self, tmp_path):
        old = tmp_path / "call-20250115.csv"
        old.write_text("timestamp\n")
        result = latest_main_log(tmp_path)
        assert result == old

    def test_prefers_newest(self, tmp_path):
        import time
        old = tmp_path / "call-20250114.csv"
        old.write_text("timestamp\n")
        time.sleep(0.05)
        session = tmp_path / "call-20250115"
        session.mkdir()
        new = session / "main.csv"
        new.write_text("timestamp\n")
        result = latest_main_log(tmp_path)
        assert result == new

    def test_ignores_related_csvs(self, tmp_path):
        (tmp_path / "call-20250115-traffic.csv").write_text("x\n")
        (tmp_path / "call-20250115-diagnostics.csv").write_text("x\n")
        assert latest_main_log(tmp_path) is None

    def test_no_logs(self, tmp_path):
        assert latest_main_log(tmp_path) is None


class TestBuildChartData:
    def test_diag_traces_by_severity(self):
        rows = [
            {"timestamp": "2025-01-15 10:00:05", "severity": "bad", "message": "Error"},
            {"timestamp": "2025-01-15 10:00:10", "severity": "warn", "message": "Warning"},
        ]
        data = build_chart_data(rows, [], "test")
        names = [t["name"] for t in data["diagTraces"]]
        assert "bad" in names
        assert "warn" in names

    def test_has_metrics_flag(self):
        assert build_chart_data(SAMPLE_DIAG, [], "t")["hasMetrics"] is False
        assert build_chart_data(SAMPLE_DIAG, [SAMPLE_MAIN_ROW], "t")["hasMetrics"] is True

    def test_panels_created_for_each_metric_group(self):
        data = build_chart_data(SAMPLE_DIAG, [SAMPLE_MAIN_ROW], "test")
        panels = data["panels"]
        titles = [p["layout"]["title"]["text"] for p in panels]
        assert "Latency" in titles
        assert "DNS" in titles
        assert "Packet Loss" in titles
        assert "WiFi Signal" in titles
        assert "SNR" in titles
        assert "TX Rate" in titles
        assert "MCS Index" in titles
        assert "System" in titles

    def test_no_panels_without_main_rows(self):
        data = build_chart_data(SAMPLE_DIAG, [], "test")
        assert data["panels"] == []

    def test_latency_panel_has_all_traces(self):
        data = build_chart_data(SAMPLE_DIAG, [SAMPLE_MAIN_ROW], "test")
        latency = [p for p in data["panels"]
                   if p["layout"]["title"]["text"] == "Latency"][0]
        names = [t["name"] for t in latency["traces"]]
        assert "Ping" in names
        assert "Gateway" in names
        assert "Jitter" in names

    def test_dns_has_own_panel(self):
        data = build_chart_data(SAMPLE_DIAG, [SAMPLE_MAIN_ROW], "test")
        dns = [p for p in data["panels"]
               if p["layout"]["title"]["text"] == "DNS"][0]
        names = [t["name"] for t in dns["traces"]]
        assert "DNS" in names

    def test_system_panel_has_cpu_and_cca(self):
        data = build_chart_data(SAMPLE_DIAG, [SAMPLE_MAIN_ROW], "test")
        system = [p for p in data["panels"]
                  if p["layout"]["title"]["text"] == "System"][0]
        names = [t["name"] for t in system["traces"]]
        assert "CPU" in names
        assert "CCA" in names

    def test_empty_diag_rows(self):
        data = build_chart_data([], [], "test")
        assert data["diagTraces"] == []
        assert data["hasMetrics"] is False


class TestBuildHtml:
    def test_contains_plotly_script(self):
        diag_rows = [{"timestamp": "2025-01-15 10:00:05", "severity": "warn",
                      "message": "Weak signal"}]
        html = build_html(diag_rows, [], "test-session")
        assert "plotly" in html.lower()
        assert "Weak signal" in html

    def test_contains_metric_data_when_main_rows(self):
        html = build_html(SAMPLE_DIAG, [SAMPLE_MAIN_ROW], "test-session")
        assert '"hasMetrics": true' in html
        assert "Latency" in html

    def test_no_metrics_without_main_rows(self):
        html = build_html(SAMPLE_DIAG, [], "test-session")
        assert '"hasMetrics": false' in html

    def test_all_severity_types(self):
        diag_rows = [
            {"timestamp": "2025-01-15 10:00:05", "severity": "bad", "message": "Bad thing"},
            {"timestamp": "2025-01-15 10:00:10", "severity": "warn", "message": "Warning"},
            {"timestamp": "2025-01-15 10:00:15", "severity": "info", "message": "Info"},
            {"timestamp": "2025-01-15 10:00:20", "severity": "resolved", "message": "Fixed"},
        ]
        html = build_html(diag_rows, [], "test")
        assert "Bad thing" in html
        assert "Warning" in html
        assert "Info" in html
        assert "Fixed" in html

    def test_html_escaping(self):
        diag_rows = [{"timestamp": "2025-01-15 10:00:05", "severity": "warn",
                      "message": '<script>alert("xss")</script>'}]
        html = build_html(diag_rows, [], "test")
        assert "<script>alert" not in html
        assert "&lt;script&gt;" in html

    def test_static_mode_no_refresh_controls(self):
        html = build_html(SAMPLE_DIAG, [], "test", live=False)
        assert "auto-refresh-toggle" not in html
        assert "fetchAndUpdate" not in html

    def test_live_mode_has_refresh_controls(self):
        html = build_html(SAMPLE_DIAG, [], "test", live=True)
        assert "auto-refresh-toggle" in html
        assert "fetchAndUpdate" in html
        assert "/api/data" in html

    def test_live_mode_has_interval_dropdown(self):
        html = build_html(SAMPLE_DIAG, [], "test", live=True)
        assert "refresh-interval" in html
        assert 'value="5"' in html
        assert 'value="10"' in html
        assert 'value="30"' in html
        assert 'value="60"' in html

    def test_live_mode_has_refresh_now_button(self):
        html = build_html(SAMPLE_DIAG, [], "test", live=True)
        assert "refresh-now" in html
        assert "Refresh now" in html

    def test_panels_container_in_html(self):
        html = build_html(SAMPLE_DIAG, [SAMPLE_MAIN_ROW], "test")
        assert "panels-container" in html
        assert "renderPanels" in html
