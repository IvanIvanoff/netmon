"""Tests for diagnostics logging and chart generation."""

import csv
from pathlib import Path

import pytest

from netmon_tui import log_diagnostics, resolve_related, DIAG_CSV_HEADER


class TestLogDiagnostics:
    def test_creates_file_with_header(self, tmp_path):
        diag_file = tmp_path / "test-diagnostics.csv"
        diag = [("warn", "Weak WiFi signal: -70 dBm")]
        result = log_diagnostics(diag_file, diag, set())
        assert diag_file.exists()
        with open(diag_file) as f:
            reader = csv.reader(f)
            header = next(reader)
            assert header == ["timestamp", "severity", "message"]
            row = next(reader)
            assert row[1] == "warn"
            assert row[2] == "Weak WiFi signal: -70 dBm"
        assert result == {("warn", "Weak WiFi signal: -70 dBm")}

    def test_skips_ok_severity(self, tmp_path):
        diag_file = tmp_path / "test-diagnostics.csv"
        diag = [("ok", "No issues detected")]
        result = log_diagnostics(diag_file, diag, set())
        assert not diag_file.exists() or diag_file.stat().st_size == 0
        assert result == set()

    def test_does_not_repeat_same_diagnostics(self, tmp_path):
        diag_file = tmp_path / "test-diagnostics.csv"
        diag = [("warn", "Weak WiFi signal: -70 dBm")]
        prev = {("warn", "Weak WiFi signal: -70 dBm")}
        result = log_diagnostics(diag_file, diag, prev)
        # File should not be created since nothing changed
        assert not diag_file.exists()
        assert result == prev

    def test_logs_new_diagnostics_only(self, tmp_path):
        diag_file = tmp_path / "test-diagnostics.csv"
        prev = {("warn", "Weak WiFi signal: -70 dBm")}
        diag = [
            ("warn", "Weak WiFi signal: -70 dBm"),
            ("bad", "High latency: 120 ms avg (last 5)"),
        ]
        result = log_diagnostics(diag_file, diag, prev)
        assert diag_file.exists()
        with open(diag_file) as f:
            reader = csv.reader(f)
            next(reader)  # header
            rows = list(reader)
        # Only the new diagnostic should be logged
        assert len(rows) == 1
        assert rows[0][1] == "bad"
        assert "High latency" in rows[0][2]

    def test_logs_resolved_when_diagnostic_disappears(self, tmp_path):
        diag_file = tmp_path / "test-diagnostics.csv"
        prev = {("warn", "Weak WiFi signal: -70 dBm"), ("bad", "High latency: 120 ms avg (last 5)")}
        diag = [("bad", "High latency: 120 ms avg (last 5)")]
        result = log_diagnostics(diag_file, diag, prev)
        with open(diag_file) as f:
            reader = csv.reader(f)
            next(reader)  # header
            rows = list(reader)
        resolved_rows = [r for r in rows if r[1] == "resolved"]
        assert len(resolved_rows) == 1
        assert "Weak WiFi" in resolved_rows[0][2]
        assert ("warn", "Weak WiFi signal: -70 dBm") not in result

    def test_appends_to_existing_file(self, tmp_path):
        diag_file = tmp_path / "test-diagnostics.csv"
        # First round
        log_diagnostics(diag_file, [("warn", "msg1")], set())
        # Second round — new diagnostic
        log_diagnostics(diag_file, [("warn", "msg1"), ("bad", "msg2")],
                        {("warn", "msg1")})
        with open(diag_file) as f:
            reader = csv.reader(f)
            next(reader)  # header
            rows = list(reader)
        assert len(rows) == 2
        assert rows[0][2] == "msg1"
        assert rows[1][2] == "msg2"

    def test_returns_empty_set_for_all_ok(self, tmp_path):
        diag_file = tmp_path / "test-diagnostics.csv"
        result = log_diagnostics(diag_file, [("ok", "No issues")], set())
        assert result == set()


class TestResolveRelatedDiagnostics:
    def test_new_format_diagnostics(self, tmp_path):
        session = tmp_path / "call-20250115"
        session.mkdir()
        main = session / "main.csv"
        result = resolve_related(main)
        assert len(result) == 5
        assert result[4] == session / "diagnostics.csv"

    def test_new_format_all_files(self, tmp_path):
        session = tmp_path / "call-20250115-100000"
        session.mkdir()
        main = session / "main.csv"
        result = resolve_related(main)
        assert result[0] == session / "traffic.csv"
        assert result[1] == session / "connections.csv"
        assert result[2] == session / "scan.csv"
        assert result[3] == session / "udp.csv"
        assert result[4] == session / "diagnostics.csv"

    def test_old_format_diagnostics(self, tmp_path):
        main = tmp_path / "call-20250115.csv"
        result = resolve_related(main)
        assert result[4] == tmp_path / "call-20250115-diagnostics.csv"

    def test_old_format_all_files(self, tmp_path):
        main = tmp_path / "call-20250115.csv"
        result = resolve_related(main)
        assert result[0] == tmp_path / "call-20250115-traffic.csv"
        assert result[1] == tmp_path / "call-20250115-connections.csv"
        assert result[2] == tmp_path / "call-20250115-scan.csv"
        assert result[3] == tmp_path / "call-20250115-udp.csv"
