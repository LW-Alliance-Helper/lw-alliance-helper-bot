"""
Unit tests for time parsing and Server Time conversion utilities.
Covers _parse_12h_time, parse_server_time, format_server_time, and
server_time_to_local.
"""
import pytest
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from tests.conftest import TEST_GUILD_ID


class TestParse12hTime:
    """Test _parse_12h_time from setup_cog."""

    def setup_method(self):
        from setup_cog import _parse_12h_time
        self.parse = _parse_12h_time

    def test_pm_time(self):
        assert self.parse("10:15pm") == "22:15"

    def test_am_time(self):
        assert self.parse("9:00am")  == "09:00"

    def test_noon(self):
        assert self.parse("12:00pm") == "12:00"

    def test_midnight(self):
        assert self.parse("12:00am") == "00:00"

    def test_no_minutes(self):
        assert self.parse("9pm")     == "21:00"

    def test_4pm(self):
        assert self.parse("4:00pm")  == "16:00"

    def test_9pm(self):
        assert self.parse("9:00pm")  == "21:00"

    def test_24h_passthrough(self):
        # _parse_12h_time only handles 12h format — 24h input returns None
        assert self.parse("22:15") is None

    def test_invalid_returns_empty(self):
        assert self.parse("not a time") is None
        assert self.parse("") is None

    def test_case_insensitive(self):
        assert self.parse("10:15PM") == "22:15"
        assert self.parse("9:00AM")  == "09:00"

    def test_with_space(self):
        assert self.parse("10:15 pm") == "22:15"


class TestParseServerTime:
    """`parse_server_time` accepts canonical HH:MM plus a few legacy forms."""

    def test_canonical_24h(self):
        from config import parse_server_time
        t = parse_server_time("23:00")
        assert t is not None
        assert (t.hour, t.minute) == (23, 0)

    def test_canonical_short_hour(self):
        from config import parse_server_time
        t = parse_server_time("9:30")
        assert t is not None
        assert (t.hour, t.minute) == (9, 30)

    def test_with_st_suffix(self):
        from config import parse_server_time
        t = parse_server_time("18:00 ST")
        assert t is not None
        assert (t.hour, t.minute) == (18, 0)

    def test_hhmm_no_separator(self):
        from config import parse_server_time
        t = parse_server_time("2300")
        assert t is not None
        assert (t.hour, t.minute) == (23, 0)

    def test_pm_short(self):
        from config import parse_server_time
        t = parse_server_time("9pm")
        assert t is not None
        assert (t.hour, t.minute) == (21, 0)

    def test_garbage_returns_none(self):
        from config import parse_server_time
        assert parse_server_time("nonsense") is None
        assert parse_server_time("") is None
        assert parse_server_time("25:00") is None
        assert parse_server_time("12:99") is None


class TestFormatServerTime:
    def test_zero_padded(self):
        from config import format_server_time
        from datetime import time
        assert format_server_time(time(9, 0))  == "09:00"
        assert format_server_time(time(23, 30)) == "23:30"


class TestServerTimeToLocal:
    """Server Time is UTC-2 (no DST). The new helper takes a stored
    `HH:MM` string + a guild timezone name and renders the local clock
    time. Output looks like `5:00 PM EST`."""

    def test_18_00_in_et(self):
        from config import server_time_to_local
        # 18:00 UTC-2 → 20:00 UTC → 4:00 PM EDT (summer) / 3:00 PM EST (winter)
        result = server_time_to_local("18:00", "America/New_York")
        assert "PM" in result
        # Either 4 (summer) or 3 (winter) depending on test run date
        assert ("4:00" in result) or ("3:00" in result)

    def test_handles_legacy_st_suffix(self):
        from config import server_time_to_local
        result = server_time_to_local("18:00 ST", "America/New_York")
        assert "PM" in result

    def test_returns_input_on_unparseable(self):
        from config import server_time_to_local
        # Garbage in → garbage out (raw string), so display still shows
        # *something* instead of disappearing.
        assert server_time_to_local("abc", "America/New_York") == "abc"

    def test_empty_input_returns_empty(self):
        from config import server_time_to_local
        assert server_time_to_local("", "America/New_York") == ""

    def test_includes_tz_abbreviation(self):
        from config import server_time_to_local
        result = server_time_to_local("18:00", "America/New_York")
        assert any(tz in result for tz in ("EDT", "EST"))

    def test_different_guild_tz_yields_different_string(self):
        from config import server_time_to_local
        et    = server_time_to_local("18:00", "America/New_York")
        london = server_time_to_local("18:00", "Europe/London")
        assert et != london
