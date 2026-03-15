"""Tests for netmon_chart.py — chart generation."""

from pathlib import Path

import pytest

from netmon_chart import (
    read_diag_csv,
    read_main_csv,
    build_html,
    resolve_diag_file,
    resolve_main_file,
    latest_main_log,
)


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
    def test_resolve_diag_file(self):
        main = Path("/logs/call-20250115.csv")
        assert resolve_diag_file(main) == Path("/logs/call-20250115-diagnostics.csv")

    def test_resolve_main_file(self):
        diag = Path("/logs/call-20250115-diagnostics.csv")
        assert resolve_main_file(diag) == Path("/logs/call-20250115.csv")


class TestLatestMainLog:
    def test_filters_diagnostics_csv(self, tmp_path):
        main = tmp_path / "call-20250115.csv"
        diag = tmp_path / "call-20250115-diagnostics.csv"
        main.write_text("timestamp\n")
        diag.write_text("timestamp,severity,message\n")
        result = latest_main_log(tmp_path)
        assert result == main

    def test_no_logs(self, tmp_path):
        assert latest_main_log(tmp_path) is None


class TestBuildHtml:
    def test_contains_plotly_script(self):
        diag_rows = [{"timestamp": "2025-01-15 10:00:05", "severity": "warn",
                      "message": "Weak signal"}]
        html = build_html(diag_rows, [], "test-session")
        assert "plotly" in html.lower()
        assert "Weak signal" in html

    def test_contains_metric_chart_when_main_rows(self):
        diag_rows = [{"timestamp": "2025-01-15 10:00:05", "severity": "warn",
                      "message": "Test"}]
        main_rows = [{"timestamp": "2025-01-15 10:00:05", "ping_avg_ms": "25.0",
                      "loss_%": "0", "rssi_dBm": "-55", "gw_ping_ms": "5",
                      "jitter_ms": "2"}]
        html = build_html(diag_rows, main_rows, "test-session")
        assert "Network Metrics" in html
        assert "hasMetrics = true" in html

    def test_no_metrics_without_main_rows(self):
        diag_rows = [{"timestamp": "2025-01-15 10:00:05", "severity": "bad",
                      "message": "Error"}]
        html = build_html(diag_rows, [], "test-session")
        assert "hasMetrics = false" in html

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
