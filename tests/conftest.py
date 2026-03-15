"""Shared fixtures for netmon tests."""

import sys
from pathlib import Path

import pytest

# Make netmon_tui importable
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

FIXTURES_DIR = Path(__file__).resolve().parent / "fixtures"


@pytest.fixture
def fixtures_dir():
    return FIXTURES_DIR


@pytest.fixture
def good_csv(fixtures_dir):
    return fixtures_dir / "main_good.csv"


@pytest.fixture
def bad_wifi_csv(fixtures_dir):
    return fixtures_dir / "main_bad_wifi.csv"


@pytest.fixture
def isp_issue_csv(fixtures_dir):
    return fixtures_dir / "main_isp_issue.csv"


@pytest.fixture
def missing_fields_csv(fixtures_dir):
    return fixtures_dir / "main_missing_fields.csv"


@pytest.fixture
def traffic_csv(fixtures_dir):
    return fixtures_dir / "traffic.csv"


@pytest.fixture
def connections_csv(fixtures_dir):
    return fixtures_dir / "connections.csv"


@pytest.fixture
def scan_csv(fixtures_dir):
    return fixtures_dir / "scan.csv"


@pytest.fixture
def udp_csv(fixtures_dir):
    return fixtures_dir / "udp.csv"


@pytest.fixture
def diag_csv(fixtures_dir):
    return fixtures_dir / "diagnostics.csv"


@pytest.fixture
def empty_csv(tmp_path):
    f = tmp_path / "empty.csv"
    f.write_text("")
    return f


@pytest.fixture
def header_only_csv(tmp_path):
    f = tmp_path / "header_only.csv"
    f.write_text(
        "timestamp,ssid,channel,rssi_dBm,noise_dBm,snr_dB,tx_rate_Mbps,"
        "interface,local_ip,public_ip,ping_target,loss_%,ping_min_ms,"
        "ping_avg_ms,ping_max_ms,dns_ms,gateway_ip,gw_ping_ms,jitter_ms,"
        "bssid,mcs,channel_band,channel_width,if_ierrs,if_oerrs,cpu_usage,"
        "mem_pressure,awdl_status,cca_pct\n"
    )
    return f
