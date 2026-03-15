"""Tests for netmon_chart.py — chart generation."""

from pathlib import Path

import pytest

from netmon_common import (
    read_csv_rows,
    resolve_diag_file,
    resolve_main_file,
    latest_main_log,
    tag_vpn,
)
from netmon_chart import (
    build_html,
    build_chart_data,
    _port_service,
    _port_label,
    _aggregate_by_port,
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
        rows = read_csv_rows(diag_csv)
        assert len(rows) == 6
        assert rows[0]["severity"] == "warn"
        assert rows[1]["severity"] == "bad"
        assert rows[2]["severity"] == "resolved"

    def test_missing_file(self, tmp_path):
        rows = read_csv_rows(tmp_path / "nonexistent.csv")
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


class TestTagVpn:
    def test_known_vpn_processes(self):
        assert tag_vpn("nordvpnd") == "[VPN] nordvpnd"
        assert tag_vpn("NordVPN") == "[VPN] NordVPN"
        assert tag_vpn("openvpn") == "[VPN] openvpn"
        assert tag_vpn("wireguard-go") == "[VPN] wireguard-go"
        assert tag_vpn("tailscaled") == "[VPN] tailscaled"
        assert tag_vpn("vpnagentd") == "[VPN] vpnagentd"

    def test_non_vpn_unchanged(self):
        assert tag_vpn("Google Chrome") == "Google Chrome"
        assert tag_vpn("Spotify") == "Spotify"
        assert tag_vpn("kernel") == "kernel"

    def test_case_insensitive(self):
        assert tag_vpn("NordVPND") == "[VPN] NordVPND"
        assert tag_vpn("OPENVPN") == "[VPN] OPENVPN"

    def test_unknown(self):
        assert tag_vpn("unknown") == "unknown"


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


SAMPLE_CONN_ROW = {
    "sample_ts": "2025-01-15 10:00:05", "process": "chrome", "pid": "123",
    "remote_ip": "142.250.80.14", "remote_port": "443",
    "bytes_in": "1000", "bytes_out": "500", "retransmits": "0",
}


class TestPortService:
    def test_well_known_ports(self):
        assert _port_service("443") == "HTTPS"
        assert _port_service("53") == "DNS"
        assert _port_service("80") == "HTTP"
        assert _port_service("22") == "SSH"

    def test_google_meet_range(self):
        assert _port_service("19302") == "Google Meet"
        assert _port_service("19305") == "Google Meet"
        assert _port_service("19309") == "Google Meet"

    def test_zoom_range(self):
        assert _port_service("8801") == "Zoom"
        assert _port_service("8810") == "Zoom"

    def test_stun_turn(self):
        assert _port_service("3478") == "STUN/TURN"
        assert _port_service("3481") == "STUN/TURN"

    def test_facetime_rtp(self):
        assert _port_service("16384") == "FaceTime/RTP"

    def test_unknown_port(self):
        assert _port_service("12345") == ""

    def test_invalid_input(self):
        assert _port_service("abc") == ""
        assert _port_service("") == ""


class TestPortLabel:
    def test_range_service_grouped(self):
        assert _port_label("19302") == "Google Meet"
        assert _port_label("8805") == "Zoom"

    def test_single_port_service_labeled(self):
        assert _port_label("443") == ":443 (HTTPS)"
        assert _port_label("53") == ":53 (DNS)"

    def test_unknown_port_bare(self):
        assert _port_label("12345") == ":12345"

    def test_invalid_input(self):
        assert _port_label("abc") == ":abc"


class TestAggregateByPort:
    def test_groups_by_port(self):
        rows = [
            {"sample_ts": "2025-01-15 10:00:05", "remote_port": "443",
             "bytes_in": "100", "bytes_out": "50", "retransmits": "0"},
            {"sample_ts": "2025-01-15 10:00:10", "remote_port": "443",
             "bytes_in": "200", "bytes_out": "100", "retransmits": "1"},
            {"sample_ts": "2025-01-15 10:00:05", "remote_port": "19302",
             "bytes_in": "500", "bytes_out": "300", "retransmits": "0"},
        ]
        result = _aggregate_by_port(rows)
        labels = [r["port"] for r in result]
        assert "Google Meet" in labels
        assert ":443 (HTTPS)" in labels

    def test_merges_meet_port_range(self):
        rows = [
            {"sample_ts": "2025-01-15 10:00:05", "remote_port": "19302",
             "bytes_in": "100", "bytes_out": "50", "retransmits": "0"},
            {"sample_ts": "2025-01-15 10:00:05", "remote_port": "19305",
             "bytes_in": "200", "bytes_out": "100", "retransmits": "0"},
        ]
        result = _aggregate_by_port(rows)
        assert len(result) == 1
        assert result[0]["port"] == "Google Meet"
        assert result[0]["bytes_in"] == 300
        assert result[0]["bytes_out"] == 150

    def test_empty_rows(self):
        assert _aggregate_by_port([]) == []

    def test_port_traffic_in_chart_data(self):
        data = build_chart_data([], [], "test", conn_rows=[SAMPLE_CONN_ROW])
        assert len(data["portTraffic"]) == 1
        assert data["portTraffic"][0]["port"] == ":443 (HTTPS)"

    def test_port_traffic_has_series(self):
        rows = [
            {"sample_ts": "2025-01-15 10:00:05", "remote_port": "19302",
             "bytes_in": "100", "bytes_out": "50", "retransmits": "0"},
            {"sample_ts": "2025-01-15 10:00:10", "remote_port": "19302",
             "bytes_in": "200", "bytes_out": "100", "retransmits": "0"},
        ]
        result = _aggregate_by_port(rows)
        assert len(result[0]["series_ts"]) == 2
        assert result[0]["series_in"] == [100, 200]


SAMPLE_TRAFFIC_ROWS = [
    {"sample_ts": "2025-01-15 10:00:05", "process": "chrome", "pid": "1",
     "bytes_in": "10240", "bytes_out": "5120", "packets_in": "10",
     "packets_out": "5", "rx_dupe": "0", "rx_ooo": "0", "retransmits": "0"},
    {"sample_ts": "2025-01-15 10:00:05", "process": "slack", "pid": "2",
     "bytes_in": "2048", "bytes_out": "1024", "packets_in": "2",
     "packets_out": "1", "rx_dupe": "0", "rx_ooo": "0", "retransmits": "0"},
    {"sample_ts": "2025-01-15 10:00:07", "process": "chrome", "pid": "1",
     "bytes_in": "20480", "bytes_out": "10240", "packets_in": "20",
     "packets_out": "10", "rx_dupe": "0", "rx_ooo": "0", "retransmits": "0"},
]


class TestThroughputPanel:
    def test_throughput_panel_present(self):
        data = build_chart_data([], [], "test",
                                traffic_rows=SAMPLE_TRAFFIC_ROWS)
        titles = [p["layout"]["title"]["text"] for p in data["panels"]]
        assert "Throughput" in titles

    def test_throughput_has_in_out_traces(self):
        data = build_chart_data([], [], "test",
                                traffic_rows=SAMPLE_TRAFFIC_ROWS)
        tp = [p for p in data["panels"]
              if p["layout"]["title"]["text"] == "Throughput"][0]
        names = [t["name"] for t in tp["traces"]]
        assert "In" in names
        assert "Out" in names

    def test_throughput_sums_across_processes(self):
        data = build_chart_data([], [], "test",
                                traffic_rows=SAMPLE_TRAFFIC_ROWS)
        tp = [p for p in data["panels"]
              if p["layout"]["title"]["text"] == "Throughput"][0]
        in_trace = [t for t in tp["traces"] if t["name"] == "In"][0]
        # First timestamp: chrome 10240 + slack 2048 = 12288 bytes = 12 KB
        assert abs(in_trace["y"][0] - 12288 / 1024) < 0.01

    def test_no_throughput_without_traffic(self):
        data = build_chart_data([], [], "test")
        titles = [p["layout"]["title"]["text"] for p in data["panels"]]
        assert "Throughput" not in titles


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

    def test_port_service_table_in_html(self):
        html = build_html(SAMPLE_DIAG, [], "test",
                          conn_rows=[SAMPLE_CONN_ROW])
        assert "Port / Service" in html
        assert "portTraffic" in html
