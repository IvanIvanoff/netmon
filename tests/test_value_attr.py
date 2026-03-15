"""Tests for value_attr threshold coloring logic."""

import pytest
from netmon_tui import value_attr


# Build a fake theme with distinguishable values
THEME = {
    "ok": "OK",
    "warn": "WARN",
    "bad": "BAD",
    "dim": "DIM",
    "text": "TEXT",
}


class TestPingThresholds:
    """Ping: >100 bad, >50 warn, <=50 ok."""

    def test_none(self):
        assert value_attr(THEME, "ping", None) == "DIM"

    def test_ok(self):
        assert value_attr(THEME, "ping", 12.0) == "OK"

    def test_ok_boundary(self):
        assert value_attr(THEME, "ping", 50.0) == "OK"

    def test_warn(self):
        assert value_attr(THEME, "ping", 75.0) == "WARN"

    def test_warn_boundary(self):
        assert value_attr(THEME, "ping", 100.0) == "WARN"

    def test_bad(self):
        assert value_attr(THEME, "ping", 150.0) == "BAD"


class TestLossThresholds:
    """Loss: >5 bad, >0 warn, 0 ok."""

    def test_ok(self):
        assert value_attr(THEME, "loss", 0.0) == "OK"

    def test_warn(self):
        assert value_attr(THEME, "loss", 2.0) == "WARN"

    def test_bad(self):
        assert value_attr(THEME, "loss", 10.0) == "BAD"


class TestRssiThresholds:
    """RSSI: <-72 bad, <-60 warn, >=-60 ok (inverted)."""

    def test_excellent(self):
        assert value_attr(THEME, "rssi", -40.0) == "OK"

    def test_good(self):
        assert value_attr(THEME, "rssi", -55.0) == "OK"

    def test_warn(self):
        assert value_attr(THEME, "rssi", -65.0) == "WARN"

    def test_bad(self):
        assert value_attr(THEME, "rssi", -80.0) == "BAD"


class TestSnrThresholds:
    """SNR: <15 bad, <25 warn, >=25 ok (inverted)."""

    def test_excellent(self):
        assert value_attr(THEME, "snr", 50.0) == "OK"

    def test_good(self):
        assert value_attr(THEME, "snr", 30.0) == "OK"

    def test_warn(self):
        assert value_attr(THEME, "snr", 20.0) == "WARN"

    def test_bad(self):
        assert value_attr(THEME, "snr", 10.0) == "BAD"


class TestTxThresholds:
    """TX: <20 bad, <50 warn, >=50 ok (inverted)."""

    def test_excellent(self):
        assert value_attr(THEME, "tx", 800.0) == "OK"

    def test_ok(self):
        assert value_attr(THEME, "tx", 54.0) == "OK"

    def test_warn(self):
        assert value_attr(THEME, "tx", 30.0) == "WARN"

    def test_bad(self):
        assert value_attr(THEME, "tx", 15.0) == "BAD"


class TestDnsThresholds:
    """DNS: >200 bad, >80 warn, <=80 ok."""

    def test_ok(self):
        assert value_attr(THEME, "dns", 25.0) == "OK"

    def test_warn(self):
        assert value_attr(THEME, "dns", 120.0) == "WARN"

    def test_bad(self):
        assert value_attr(THEME, "dns", 250.0) == "BAD"


class TestGatewayThresholds:
    """Gateway: >20 bad, >5 warn, <=5 ok."""

    def test_ok(self):
        assert value_attr(THEME, "gw", 1.5) == "OK"

    def test_warn(self):
        assert value_attr(THEME, "gw", 10.0) == "WARN"

    def test_bad(self):
        assert value_attr(THEME, "gw", 25.0) == "BAD"


class TestJitterThresholds:
    """Jitter: >30 bad, >10 warn, <=10 ok."""

    def test_ok(self):
        assert value_attr(THEME, "jitter", 2.0) == "OK"

    def test_warn(self):
        assert value_attr(THEME, "jitter", 15.0) == "WARN"

    def test_bad(self):
        assert value_attr(THEME, "jitter", 40.0) == "BAD"


class TestCpuThresholds:
    """CPU: >300 bad, >150 warn, <=150 ok."""

    def test_ok(self):
        assert value_attr(THEME, "cpu", 45.0) == "OK"

    def test_warn(self):
        assert value_attr(THEME, "cpu", 200.0) == "WARN"

    def test_bad(self):
        assert value_attr(THEME, "cpu", 400.0) == "BAD"


class TestMemThresholds:
    """Memory: >90 bad, >80 warn, <=80 ok."""

    def test_ok(self):
        assert value_attr(THEME, "mem", 60.0) == "OK"

    def test_warn(self):
        assert value_attr(THEME, "mem", 85.0) == "WARN"

    def test_bad(self):
        assert value_attr(THEME, "mem", 95.0) == "BAD"


class TestUnknownKind:
    def test_unknown_returns_text(self):
        assert value_attr(THEME, "unknown_metric", 42.0) == "TEXT"
