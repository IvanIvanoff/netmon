"""Tests for CSV parsing functions in netmon_tui.py."""

import pytest
from pathlib import Path

from netmon_tui import (
    parse_main_csv,
    parse_traffic_totals,
    top_traffic_rows,
    parse_connection_totals,
    top_connection_rows,
    parse_udp_totals,
    top_udp_rows,
    subtract_udp_totals,
    parse_scan_csv,
    subtract_traffic_totals,
    subtract_connection_totals,
)


# ---------------------------------------------------------------------------
# parse_main_csv
# ---------------------------------------------------------------------------

class TestParseMainCsv:
    def test_good_csv_sample_count(self, good_csv):
        result = parse_main_csv(good_csv)
        assert result["samples"] == 5

    def test_good_csv_timestamps(self, good_csv):
        result = parse_main_csv(good_csv)
        assert result["first_ts"] == "2026-03-12 14:00:00"
        assert result["last_ts"] == "2026-03-12 14:00:08"

    def test_good_csv_ping_target(self, good_csv):
        result = parse_main_csv(good_csv)
        assert result["ping_target"] == "8.8.8.8"

    def test_good_csv_ping_values(self, good_csv):
        result = parse_main_csv(good_csv)
        assert len(result["ping_vals"]) == 5
        assert all(10.0 <= v <= 15.0 for v in result["ping_vals"])

    def test_good_csv_loss_all_zero(self, good_csv):
        result = parse_main_csv(good_csv)
        assert all(v == 0.0 for v in result["loss_vals"])

    def test_good_csv_rssi_values(self, good_csv):
        result = parse_main_csv(good_csv)
        assert len(result["rssi_vals"]) == 5
        assert all(-50 <= v <= -40 for v in result["rssi_vals"])

    def test_good_csv_snr_values(self, good_csv):
        result = parse_main_csv(good_csv)
        assert all(45 <= v <= 55 for v in result["snr_vals"])

    def test_good_csv_tx_values(self, good_csv):
        result = parse_main_csv(good_csv)
        assert all(v >= 700 for v in result["tx_vals"])

    def test_good_csv_gateway_ping(self, good_csv):
        result = parse_main_csv(good_csv)
        assert len(result["gw_ping_vals"]) == 5
        assert all(v < 5 for v in result["gw_ping_vals"])

    def test_good_csv_jitter(self, good_csv):
        result = parse_main_csv(good_csv)
        assert all(v < 2 for v in result["jitter_vals"])

    def test_good_csv_cpu_mem(self, good_csv):
        result = parse_main_csv(good_csv)
        assert len(result["cpu_vals"]) == 5
        assert len(result["mem_vals"]) == 5

    def test_good_csv_bssid_set(self, good_csv):
        result = parse_main_csv(good_csv)
        assert result["bssid_set"] == {"aa:bb:cc:dd:ee:ff"}

    def test_good_csv_channel_set(self, good_csv):
        result = parse_main_csv(good_csv)
        assert result["channel_set"] == {"36"}

    def test_good_csv_latest_row(self, good_csv):
        result = parse_main_csv(good_csv)
        assert result["latest"]["timestamp"] == "2026-03-12 14:00:08"
        assert result["latest"]["ssid"] == "MyNetwork"
        assert result["latest"]["interface"] == "en0"

    def test_bad_wifi_csv(self, bad_wifi_csv):
        result = parse_main_csv(bad_wifi_csv)
        assert result["samples"] == 5
        # Bad wifi should have low RSSI
        assert all(v < -75 for v in result["rssi_vals"])
        # High loss
        assert all(v >= 10 for v in result["loss_vals"])
        # High latency
        assert all(v > 100 for v in result["ping_vals"])
        # AP roaming (2 BSSIDs)
        assert len(result["bssid_set"]) == 2

    def test_isp_issue_csv(self, isp_issue_csv):
        result = parse_main_csv(isp_issue_csv)
        # Good RSSI but high latency → ISP issue
        assert all(v > -50 for v in result["rssi_vals"])
        assert all(v > 50 for v in result["ping_vals"])
        # Low gateway ping → WiFi is fine
        assert all(v < 5 for v in result["gw_ping_vals"])

    def test_missing_fields_csv(self, missing_fields_csv):
        result = parse_main_csv(missing_fields_csv)
        assert result["samples"] == 2
        # All numeric lists should be empty (fields are "?")
        assert result["ping_vals"] == []
        assert result["rssi_vals"] == []
        assert result["loss_vals"] == []
        assert result["snr_vals"] == []
        assert result["dns_vals"] == []

    def test_empty_csv(self, empty_csv):
        result = parse_main_csv(empty_csv)
        assert result["samples"] == 0
        assert result["first_ts"] == "n/a"
        assert result["last_ts"] == "n/a"

    def test_header_only(self, header_only_csv):
        result = parse_main_csv(header_only_csv)
        assert result["samples"] == 0

    def test_nonexistent_file(self, tmp_path):
        result = parse_main_csv(tmp_path / "nope.csv")
        assert result["samples"] == 0
        assert result["first_ts"] == "n/a"

    def test_interface_errors(self, bad_wifi_csv):
        result = parse_main_csv(bad_wifi_csv)
        assert len(result["if_ierrs_vals"]) == 5
        assert len(result["if_oerrs_vals"]) == 5
        assert sum(result["if_ierrs_vals"]) > 0

    def test_dns_values(self, good_csv):
        result = parse_main_csv(good_csv)
        assert len(result["dns_vals"]) == 5
        assert all(v < 100 for v in result["dns_vals"])


# ---------------------------------------------------------------------------
# parse_traffic_totals
# ---------------------------------------------------------------------------

class TestParseTrafficTotals:
    def test_process_counts(self, traffic_csv):
        totals = parse_traffic_totals(traffic_csv)
        assert "Google Chrome" in totals
        assert "Spotify" in totals
        assert "Slack" in totals
        assert "kernel" in totals

    def test_bytes_summed(self, traffic_csv):
        totals = parse_traffic_totals(traffic_csv)
        chrome = totals["Google Chrome"]
        # 5242880 + 6291456 = 11534336 bytes in
        assert chrome[0] == 5242880 + 6291456
        # 1048576 + 1572864 = 2621440 bytes out
        assert chrome[1] == 1048576 + 1572864

    def test_retransmits_summed(self, traffic_csv):
        totals = parse_traffic_totals(traffic_csv)
        spotify = totals["Spotify"]
        assert spotify[2] == 5 + 8  # retransmits

    def test_empty_file(self, empty_csv):
        assert parse_traffic_totals(empty_csv) == {}

    def test_nonexistent(self, tmp_path):
        assert parse_traffic_totals(tmp_path / "nope.csv") == {}


class TestTopTrafficRows:
    def test_sorted_by_total(self, traffic_csv):
        totals = parse_traffic_totals(traffic_csv)
        rows = top_traffic_rows(totals)
        # Chrome should be first (highest total)
        assert rows[0][0] == "Google Chrome"
        # Verify descending order
        for i in range(len(rows) - 1):
            assert rows[i][1] + rows[i][2] >= rows[i + 1][1] + rows[i + 1][2]

    def test_max_10_rows(self):
        # Create 15 fake processes
        totals = {f"proc{i}": [i * 1000, i * 500, 0] for i in range(15)}
        rows = top_traffic_rows(totals)
        assert len(rows) == 10


# ---------------------------------------------------------------------------
# parse_connection_totals
# ---------------------------------------------------------------------------

class TestParseConnectionTotals:
    def test_connection_keys(self, connections_csv):
        totals = parse_connection_totals(connections_csv)
        assert ("Google Chrome", "142.250.80.46") in totals
        assert ("Google Chrome", "142.250.80.100") in totals
        assert ("Spotify", "35.186.224.25") in totals

    def test_bytes_summed(self, connections_csv):
        totals = parse_connection_totals(connections_csv)
        chrome_main = totals[("Google Chrome", "142.250.80.46")]
        assert chrome_main[0] == 3145728 + 4194304  # bytes in
        assert chrome_main[1] == 786432 + 1048576  # bytes out

    def test_retransmits(self, connections_csv):
        totals = parse_connection_totals(connections_csv)
        spotify = totals[("Spotify", "35.186.224.25")]
        assert spotify[2] == 5 + 8


class TestTopConnectionRows:
    def test_sorted_by_total(self, connections_csv):
        totals = parse_connection_totals(connections_csv)
        rows = top_connection_rows(totals)
        for i in range(len(rows) - 1):
            assert rows[i][2] + rows[i][3] >= rows[i + 1][2] + rows[i + 1][3]


# ---------------------------------------------------------------------------
# parse_scan_csv
# ---------------------------------------------------------------------------

class TestParseScanCsv:
    def test_returns_latest_scan(self, scan_csv):
        rows = parse_scan_csv(scan_csv)
        # Second scan has 6 networks, first has 5
        assert len(rows) == 6
        # All rows should be from the latest timestamp
        assert all(r["scan_ts"] == "2026-03-12 14:00:15" for r in rows)

    def test_channel_values(self, scan_csv):
        rows = parse_scan_csv(scan_csv)
        channels = [r["channel"] for r in rows]
        assert "36 (5GHz; 80MHz)" in channels
        assert "1 (2.4GHz; 20MHz)" in channels

    def test_security_values(self, scan_csv):
        rows = parse_scan_csv(scan_csv)
        securities = {r["security"] for r in rows}
        assert "WPA2 Personal" in securities

    def test_empty_file(self, empty_csv):
        assert parse_scan_csv(empty_csv) == []

    def test_nonexistent(self, tmp_path):
        assert parse_scan_csv(tmp_path / "nope.csv") == []


# ---------------------------------------------------------------------------
# subtract_traffic_totals / subtract_connection_totals
# ---------------------------------------------------------------------------

class TestSubtractTotals:
    def test_traffic_subtraction(self):
        current = {
            "Chrome": [10000, 5000, 10],
            "Slack": [3000, 1000, 0],
        }
        baseline = {
            "Chrome": [4000, 2000, 3],
            "Slack": [3000, 1000, 0],  # no change → excluded
        }
        result = subtract_traffic_totals(current, baseline)
        assert "Chrome" in result
        assert result["Chrome"] == [6000, 3000, 7]
        # Slack had no change, should not appear
        assert "Slack" not in result

    def test_negative_clamp(self):
        current = {"proc": [100, 200, 0]}
        baseline = {"proc": [500, 300, 5]}
        result = subtract_traffic_totals(current, baseline)
        # All deltas negative → clamped to 0 → excluded
        assert "proc" not in result

    def test_new_process(self):
        current = {"new_proc": [1000, 500, 0]}
        baseline = {}
        result = subtract_traffic_totals(current, baseline)
        assert "new_proc" in result
        assert result["new_proc"] == [1000, 500, 0]

    def test_connection_subtraction(self):
        current = {("Chrome", "1.2.3.4"): [10000, 5000, 2]}
        baseline = {("Chrome", "1.2.3.4"): [4000, 2000, 1]}
        result = subtract_connection_totals(current, baseline)
        assert result[("Chrome", "1.2.3.4")] == [6000, 3000, 1]


# ---------------------------------------------------------------------------
# parse_udp_totals / top_udp_rows
# ---------------------------------------------------------------------------

class TestParseUdpTotals:
    def test_basic_parsing(self, udp_csv):
        totals = parse_udp_totals(udp_csv)
        assert "zoom.us" in totals
        assert "Google Chrome" in totals
        # zoom.us: 80000+90000=170000 in, 25000+28000=53000 out
        assert totals["zoom.us"][0] == 170000
        assert totals["zoom.us"][1] == 53000

    def test_missing_file(self, tmp_path):
        totals = parse_udp_totals(tmp_path / "nonexistent.csv")
        assert totals == {}

    def test_empty_file(self, empty_csv):
        totals = parse_udp_totals(empty_csv)
        assert totals == {}


class TestTopUdpRows:
    def test_sorted_by_total(self, udp_csv):
        totals = parse_udp_totals(udp_csv)
        rows = top_udp_rows(totals)
        assert len(rows) > 0
        # zoom.us has most traffic (223000 total), should be first
        assert rows[0][0] == "zoom.us"

    def test_limit_to_10(self):
        totals = {f"proc{i}": [1000 * i, 500 * i] for i in range(15)}
        rows = top_udp_rows(totals)
        assert len(rows) == 10


class TestSubtractUdpTotals:
    def test_basic_subtraction(self):
        current = {"zoom.us": [100000, 50000], "Chrome": [20000, 10000]}
        baseline = {"zoom.us": [80000, 25000]}
        result = subtract_udp_totals(current, baseline)
        assert result["zoom.us"] == [20000, 25000]
        assert result["Chrome"] == [20000, 10000]

    def test_empty_baseline(self):
        current = {"zoom.us": [100000, 50000]}
        result = subtract_udp_totals(current, {})
        assert result == current


# ---------------------------------------------------------------------------
# CSV column count integrity
# ---------------------------------------------------------------------------

class TestCsvIntegrity:
    """Verify CSV files have consistent column counts (catches comma-in-value bugs)."""

    def _check_column_consistency(self, path: Path):
        lines = path.read_text().strip().split("\n")
        assert len(lines) >= 1, "File is empty"
        header_cols = len(lines[0].split(","))
        for i, line in enumerate(lines[1:], start=2):
            data_cols = len(line.split(","))
            assert data_cols == header_cols, (
                f"Line {i}: expected {header_cols} columns, got {data_cols}. "
                f"Line content: {line!r}"
            )

    def test_main_good(self, good_csv):
        self._check_column_consistency(good_csv)

    def test_main_bad_wifi(self, bad_wifi_csv):
        self._check_column_consistency(bad_wifi_csv)

    def test_main_isp_issue(self, isp_issue_csv):
        self._check_column_consistency(isp_issue_csv)

    def test_traffic(self, traffic_csv):
        self._check_column_consistency(traffic_csv)

    def test_connections(self, connections_csv):
        self._check_column_consistency(connections_csv)

    def test_scan(self, scan_csv):
        self._check_column_consistency(scan_csv)

    def test_udp(self, udp_csv):
        self._check_column_consistency(udp_csv)

    def test_main_has_29_columns(self, good_csv):
        header = good_csv.read_text().split("\n")[0]
        assert len(header.split(",")) == 29
