"""Tests for pure helper functions in netmon_tui.py."""

import pytest

from netmon_tui import (
    avg,
    calc_duration,
    fmt_num,
    human_bytes,
    na_style,
    sparkline,
    to_float,
    to_int,
)


# ---------------------------------------------------------------------------
# to_float
# ---------------------------------------------------------------------------

class TestToFloat:
    def test_valid_int(self):
        assert to_float("42") == 42.0

    def test_valid_float(self):
        assert to_float("3.14") == pytest.approx(3.14)

    def test_negative(self):
        assert to_float("-75") == -75.0

    def test_whitespace(self):
        assert to_float("  12.5  ") == 12.5

    def test_question_mark(self):
        assert to_float("?") is None

    def test_empty(self):
        assert to_float("") is None

    def test_none(self):
        assert to_float(None) is None

    def test_garbage(self):
        assert to_float("abc") is None

    def test_zero(self):
        assert to_float("0") == 0.0

    def test_zero_float(self):
        assert to_float("0.0") == 0.0


# ---------------------------------------------------------------------------
# to_int
# ---------------------------------------------------------------------------

class TestToInt:
    def test_valid(self):
        assert to_int("42") == 42

    def test_float_truncates(self):
        assert to_int("3.7") == 3

    def test_invalid(self):
        assert to_int("?") == 0

    def test_empty(self):
        assert to_int("") == 0

    def test_negative(self):
        assert to_int("-5") == -5


# ---------------------------------------------------------------------------
# avg
# ---------------------------------------------------------------------------

class TestAvg:
    def test_basic(self):
        assert avg([1.0, 2.0, 3.0]) == pytest.approx(2.0)

    def test_single(self):
        assert avg([5.0]) == 5.0

    def test_empty(self):
        assert avg([]) is None

    def test_negative(self):
        assert avg([-10.0, -20.0]) == pytest.approx(-15.0)


# ---------------------------------------------------------------------------
# human_bytes
# ---------------------------------------------------------------------------

class TestHumanBytes:
    def test_bytes(self):
        assert human_bytes(500) == "500 B"

    def test_zero(self):
        assert human_bytes(0) == "0 B"

    def test_negative_clamps(self):
        assert human_bytes(-100) == "0 B"

    def test_kilobytes(self):
        assert human_bytes(1536) == "1.5 KB"

    def test_megabytes(self):
        assert human_bytes(5 * 1024 * 1024) == "5.0 MB"

    def test_gigabytes(self):
        assert human_bytes(2 * 1024**3) == "2.0 GB"

    def test_just_over_kb(self):
        assert human_bytes(1024) == "1.0 KB"

    def test_just_over_mb(self):
        assert human_bytes(1024 * 1024) == "1.0 MB"


# ---------------------------------------------------------------------------
# fmt_num
# ---------------------------------------------------------------------------

class TestFmtNum:
    def test_none(self):
        assert fmt_num(None) == "n/a"

    def test_none_with_suffix(self):
        assert fmt_num(None, " ms") == "n/a"

    def test_small_float(self):
        assert fmt_num(12.3, " ms") == "12.3 ms"

    def test_large_float(self):
        assert fmt_num(150.7, " ms") == "151 ms"

    def test_exactly_100(self):
        assert fmt_num(100.0, "%") == "100%"

    def test_zero(self):
        assert fmt_num(0.0, " ms") == "0.0 ms"

    def test_negative(self):
        assert fmt_num(-72.5, " dBm") == "-72.5 dBm"

    def test_negative_large(self):
        assert fmt_num(-120.0, " dBm") == "-120 dBm"


# ---------------------------------------------------------------------------
# calc_duration
# ---------------------------------------------------------------------------

class TestCalcDuration:
    def test_seconds(self):
        assert calc_duration("2026-03-12 14:00:00", "2026-03-12 14:00:45") == "45s"

    def test_minutes(self):
        assert calc_duration("2026-03-12 14:00:00", "2026-03-12 14:05:30") == "5m 30s"

    def test_hours(self):
        assert calc_duration("2026-03-12 14:00:00", "2026-03-12 15:30:15") == "1h 30m 15s"

    def test_zero_duration(self):
        assert calc_duration("2026-03-12 14:00:00", "2026-03-12 14:00:00") == "0s"

    def test_negative_returns_na(self):
        assert calc_duration("2026-03-12 14:05:00", "2026-03-12 14:00:00") == "n/a"

    def test_na_input(self):
        assert calc_duration("n/a", "n/a") == "n/a"

    def test_bad_format(self):
        assert calc_duration("not-a-date", "also-not") == "n/a"

    def test_mixed_na(self):
        assert calc_duration("2026-03-12 14:00:00", "n/a") == "n/a"


# ---------------------------------------------------------------------------
# sparkline
# ---------------------------------------------------------------------------

class TestSparkline:
    def test_empty(self):
        assert sparkline([]) == ""

    def test_single_value(self):
        result = sparkline([5.0])
        assert len(result) == 1

    def test_length_respects_width(self):
        vals = list(range(50))
        result = sparkline([float(v) for v in vals], width=10)
        assert len(result) == 10

    def test_all_same_values(self):
        # All same → all same character (first spark char since range is 0)
        result = sparkline([5.0, 5.0, 5.0, 5.0])
        assert len(set(result)) == 1

    def test_ascending(self):
        result = sparkline([1.0, 2.0, 3.0, 4.0, 5.0])
        # First char should be lowest, last should be highest
        assert result[0] <= result[-1]

    def test_uses_unicode_blocks(self):
        result = sparkline([0.0, 100.0])
        for ch in result:
            assert ch in " ▁▂▃▄▅▆▇█"

    def test_negative_values(self):
        result = sparkline([-80.0, -60.0, -40.0])
        assert len(result) == 3
        # Should still produce valid sparkline
        assert result[0] <= result[-1]


# ---------------------------------------------------------------------------
# na_style
# ---------------------------------------------------------------------------

class TestNaStyle:
    def test_na_returns_dim(self):
        theme = {"dim": 99, "text": 0}
        assert na_style(theme, "n/a", 42) == 99

    def test_value_returns_normal(self):
        theme = {"dim": 99, "text": 0}
        assert na_style(theme, "12.3 ms", 42) == 42

    def test_empty_returns_normal(self):
        theme = {"dim": 99, "text": 0}
        assert na_style(theme, "", 42) == 42
